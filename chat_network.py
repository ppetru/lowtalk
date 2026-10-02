"""Nonblocking TCP mesh, driven by tick() on the UI thread.

No worker threads touch curses. Each tick does bounded socket work so a busy or
slow peer cannot monopolize the input loop. Events are synchronous callbacks.
All deadlines use monotonic time; wall-clock adjustments cannot alter them.

Online means a committed protocol-v2 handshake, not Tailscale device presence.
The smaller process instance selects the first valid candidate in either socket
direction. Routine connection changes are quiet; only a newly committed remote
instance adds a history boundary. There is no offline queue or delivery receipt.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
import errno
import math
import random
import secrets
import selectors
import socket
import time
from typing import Callable, NoReturn, Optional, cast

from chat_config import PeerConfig
from chat_protocol import Frame, FrameReader, ProtocolError, encode

CONNECT_TIMEOUT = 5.0
HEARTBEAT_INTERVAL = 10.0
DEAD_TIMEOUT = 30.0
MAX_PENDING = 128 * 1024
SHUTDOWN_TIMEOUT = 1.0
# Linux may return pending network errors from accept(); macOS also reports
# aborted connections. Retry only these transient errors, not broken listeners.
_RETRYABLE_ACCEPT_ERRORS = {
    getattr(errno, name) for name in (
        "ECONNABORTED", "EINTR", "ENETDOWN", "EPROTO", "ENOPROTOOPT",
        "EHOSTDOWN", "ENONET", "EHOSTUNREACH", "EOPNOTSUPP", "ENETUNREACH",
    ) if hasattr(errno, name)
}


def _unreachable(value: NoReturn) -> NoReturn:
    raise AssertionError(f"unhandled state or frame: {value!r}")


class Phase(Enum):
    CONNECTING = auto()
    HELLO = auto()
    CANDIDATE = auto()
    SELECTED = auto()
    ACCEPTED = auto()
    READY = auto()


@dataclass
class Peer:
    config: PeerConfig
    connection: Optional[Connection] = None
    candidates: dict[bool, Connection] = field(default_factory=lambda: dict[bool, Connection]())
    nick: str = ""
    last_instance: Optional[str] = None
    retry_at: float = 0.0
    backoff: float = 1.0
    address_index: int = 0
    last_error: str = ""

    @property
    def status(self) -> str:
        if self.connection is not None and self.connection.ready:
            return "online"
        return "connecting" if self.candidates else "offline"

    @property
    def display_status(self) -> str:
        status = self.status
        remaining = self.retry_at - time.monotonic()
        if status == "offline" and remaining > 0 and math.isfinite(remaining):
            return f"offline (retry in {math.ceil(remaining)}s)"
        return status


@dataclass
class Connection:
    sock: socket.socket
    peer: Peer
    address: str
    outgoing: bool
    created: float
    phase: Phase = Phase.HELLO
    remote_instance: Optional[str] = None
    remote_nick: str = ""
    reader: FrameReader = field(default_factory=FrameReader)
    output: bytearray = field(default_factory=bytearray)
    # End offsets of application frames in output; protocol frames do not count.
    pending_messages: deque[int] = field(default_factory=lambda: deque[int]())
    last_received: float = 0.0
    last_ping: float = 0.0

    @property
    def ready(self) -> bool:
        return self.phase is Phase.READY


class Network:
    def __init__(
        self, nick: str, port: int, peers: list[PeerConfig], bind_ip: str,
        on_event: Callable[[str, str], None],
    ) -> None:
        self.nick = nick
        self.port = port
        self.bind_ip = bind_ip
        self.on_event = on_event
        self.instance = secrets.token_hex(16)
        self.peers = [Peer(config) for config in peers]
        self.allowed = {ip: peer for peer in self.peers for ip in peer.config.addresses}
        self.selector = selectors.DefaultSelector()
        self.connections: dict[socket.socket, Connection] = {}
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind((bind_ip, port))
            self.listener.listen(16)
            self.listener.setblocking(False)
            self.selector.register(self.listener, selectors.EVENT_READ)
        except OSError:
            self.listener.close()
            self.selector.close()
            raise

    def close(self) -> None:
        for conn in list(self.connections.values()):
            self._retire(conn)
        self.listener.close()
        self.selector.close()

    def flush(self, timeout: float = SHUTDOWN_TIMEOUT) -> list[str]:
        """Boundedly drain committed sockets, without dialing or heartbeats.

        Return peers whose application data remains at the deadline or was
        discarded on failure. Failures also use the ordinary warning callback.
        Sending to TCP is still not a delivery receipt.
        """
        deadline = time.monotonic() + timeout
        discarded: set[str] = set()
        while any(conn.ready and conn.output for conn in self.connections.values()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for key, mask in self.selector.select(timeout=min(remaining, 0.05)):
                if key.fileobj is self.listener:
                    continue
                conn = cast(Connection, key.data)
                if conn.ready:
                    had_pending = bool(conn.pending_messages)
                    self._service(conn, mask, time.monotonic())
                    if had_pending and not self._active(conn):
                        discarded.add(conn.peer.config.label)
        discarded.update(conn.peer.config.label for conn in self.connections.values()
                         if conn.pending_messages)
        return sorted(discarded)

    def _active(self, conn: Connection) -> bool:
        return self.connections.get(conn.sock) is conn

    def _interest(self, conn: Connection) -> None:
        if not self._active(conn):
            return
        events = selectors.EVENT_READ
        if conn.phase is Phase.CONNECTING or conn.output:
            events |= selectors.EVENT_WRITE
        self.selector.modify(conn.sock, events, conn)

    def _queue(self, conn: Connection, message: Frame) -> bool:
        if not self._active(conn):
            return False
        data = encode(message)
        if len(conn.output) + len(data) > MAX_PENDING:
            self._drop(conn, "outgoing buffer full; pending messages may be lost", warn=True)
            return False
        conn.output.extend(data)
        if message["type"] == "message":
            conn.pending_messages.append(len(conn.output))
        self._interest(conn)
        return True

    def _register(self, sock: socket.socket, peer: Peer, address: str,
                  outgoing: bool, connecting: bool = False) -> None:
        # A selection is irrevocable. Before selection retain at most one socket
        # in each direction, allowing either side to start the conversation.
        if peer.connection is not None or outgoing in peer.candidates:
            sock.close()
            return
        now = time.monotonic()
        conn = Connection(
            sock=sock, peer=peer, address=address, outgoing=outgoing,
            created=now, phase=Phase.CONNECTING if connecting else Phase.HELLO,
            last_received=now, last_ping=now,
        )
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.selector.register(sock, selectors.EVENT_READ, conn)
        self.connections[sock] = conn
        peer.candidates[outgoing] = conn
        self._queue(conn, {"type": "hello", "version": 2,
                           "nick": self.nick, "port": self.port, "instance": self.instance})

    def _retry(self, peer: Peer, now: float) -> None:
        delay = min(30.0, peer.backoff + random.uniform(0, peer.backoff / 4))
        peer.retry_at = now + delay
        peer.backoff = min(30.0, peer.backoff * 2)

    def _dial(self, peer: Peer, now: float) -> None:
        self._retry(peer, now)
        addresses = peer.config.addresses
        address = addresses[peer.address_index % len(addresses)]
        peer.address_index += 1
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setblocking(False)
            if self.bind_ip != "0.0.0.0":
                sock.bind((self.bind_ip, 0))
            result = sock.connect_ex((address, peer.config.port))
            if result not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                peer.last_error = errno.errorcode.get(result, str(result))
                sock.close()
                return
            self._register(sock, peer, address, True, connecting=result != 0)
        except OSError as exc:
            peer.last_error = str(exc)
            sock.close()

    def _accept(self) -> None:
        for _ in range(8):
            try:
                sock, (address, _) = self.listener.accept()
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno in _RETRYABLE_ACCEPT_ERRORS:
                    continue
                raise
            peer = self.allowed.get(address)
            if peer is None:
                # Do not even send a hello to an unlisted source address.
                sock.close()
                continue
            try:
                self._register(sock, peer, address, False)
            except OSError:
                sock.close()

    def _retire(self, conn: Connection) -> None:
        if not self._active(conn):
            return
        self.selector.unregister(conn.sock)
        del self.connections[conn.sock]
        conn.sock.close()
        peer = conn.peer
        assert peer.candidates.get(conn.outgoing) is conn
        del peer.candidates[conn.outgoing]
        if peer.connection is conn:
            peer.connection = None

    def _drop(self, conn: Connection, reason: str, *, warn: bool = False) -> None:
        if not self._active(conn):
            return
        was_ready = conn.ready
        had_pending = bool(conn.pending_messages)
        peer = conn.peer
        self._retire(conn)
        if not peer.candidates:
            peer.last_error = reason
            self._retry(peer, time.monotonic())
        if was_ready and had_pending:
            self.on_event("!", f"{peer.config.label}: connection lost with pending data; "
                          "some messages may not have arrived")
        if warn and was_ready:
            self.on_event("!", f"{peer.config.label}: {reason}")

    def _choose(self, conn: Connection, phase: Phase) -> None:
        peer = conn.peer
        assert self._active(conn) and conn.phase is Phase.CANDIDATE
        assert peer.connection is None and conn.remote_instance is not None
        assert phase in (Phase.SELECTED, Phase.ACCEPTED)
        peer.connection = conn
        conn.phase = phase
        other = peer.candidates.get(not conn.outgoing)
        if other is not None:
            self._retire(other)
        assert len(peer.candidates) == 1 and peer.candidates[conn.outgoing] is conn

    def _commit(self, conn: Connection) -> None:
        peer = conn.peer
        instance = conn.remote_instance
        assert self._active(conn) and peer.connection is conn
        assert conn.phase in (Phase.SELECTED, Phase.ACCEPTED)
        assert instance is not None and len(peer.candidates) == 1
        previous = peer.last_instance
        conn.phase = Phase.READY
        peer.last_instance = instance
        peer.nick = conn.remote_nick
        peer.last_error = ""
        peer.backoff = 1.0
        if previous is not None and previous != instance:
            self.on_event("---", f"{peer.nick} has a new Lowtalk session; "
                          "previous scrollback was cleared.")

    def _message(self, conn: Connection, message: Frame) -> None:
        phase = conn.phase
        if phase is Phase.HELLO:
            if message["type"] != "hello" or message["port"] != conn.peer.config.port:
                raise ProtocolError("expected hello with configured listening port")
            if message["instance"] == self.instance:
                raise ProtocolError("remote process instance matches local instance")
            conn.remote_instance = message["instance"]
            conn.remote_nick = message["nick"]
            conn.phase = Phase.CANDIDATE
            if self.instance < message["instance"]:
                self._choose(conn, Phase.SELECTED)
                self._queue(conn, {"type": "select"})
            return
        if phase is Phase.CANDIDATE:
            if message["type"] != "select":
                raise ProtocolError("expected select before application frames")
            assert conn.remote_instance is not None
            if conn.remote_instance >= self.instance:
                raise ProtocolError("only the smaller process instance may select")
            self._choose(conn, Phase.ACCEPTED)
            self._queue(conn, {"type": "accept"})
            return
        if phase is Phase.SELECTED:
            if message["type"] != "accept":
                raise ProtocolError("expected accept before application frames")
            if self._queue(conn, {"type": "ready"}):
                # ready precedes all application output on this TCP stream. The
                # follower is already locked to this socket by its accept.
                self._commit(conn)
            return
        if phase is Phase.ACCEPTED:
            if message["type"] != "ready":
                raise ProtocolError("expected ready before application frames")
            self._commit(conn)
            return
        if phase is Phase.CONNECTING:
            raise ProtocolError("expected completed TCP connection before frames")
        if phase is not Phase.READY:
            _unreachable(phase)
        if message["type"] == "message":
            self.on_event(f"{conn.peer.nick} @ {conn.address}", message["text"])
        elif message["type"] == "ping":
            self._queue(conn, {"type": "pong"})
        elif message["type"] == "pong":
            pass
        elif (message["type"] == "hello" or message["type"] == "select"
              or message["type"] == "accept" or message["type"] == "ready"):
            raise ProtocolError("unexpected handshake frame after ready")
        else:
            _unreachable(message)

    def _service(self, conn: Connection, mask: int, now: float) -> None:
        # select() returns a snapshot: choosing another socket may already have
        # retired this entry, including its file descriptor, earlier this tick.
        if not self._active(conn):
            return
        if not conn.ready and now - conn.created >= CONNECT_TIMEOUT:
            self._drop(conn, "handshake timed out")
            return
        try:
            if conn.phase is Phase.CONNECTING:
                error = conn.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if error:
                    raise OSError(error, "connect failed")
                conn.phase = Phase.HELLO
            if mask & selectors.EVENT_READ:
                data = conn.sock.recv(16_384)
                if not data:
                    self._drop(conn, "connection closed")
                    return
                conn.last_received = now
                for message in conn.reader.feed(data):
                    self._message(conn, message)
                    if not self._active(conn):
                        return
            if mask & selectors.EVENT_WRITE and conn.output:
                sent = conn.sock.send(conn.output)
                del conn.output[:sent]
                conn.pending_messages = deque(
                    end - sent for end in conn.pending_messages if end > sent
                )
            self._interest(conn)
        except BlockingIOError:
            pass
        except OSError as exc:
            self._drop(conn, str(exc))
        except ProtocolError as exc:
            self._drop(conn, str(exc), warn=True)

    def tick(self) -> None:
        now = time.monotonic()
        for key, mask in self.selector.select(timeout=0):
            if key.fileobj is self.listener:
                self._accept()
            else:
                self._service(cast(Connection, key.data), mask, now)
        for conn in list(self.connections.values()):
            if not conn.ready and now - conn.created >= CONNECT_TIMEOUT:
                self._drop(conn, "handshake timed out")
            elif now - conn.last_received > DEAD_TIMEOUT:
                self._drop(conn, "heartbeat timed out")
            elif conn.ready and now - conn.last_ping >= HEARTBEAT_INTERVAL:
                conn.last_ping = now
                self._queue(conn, {"type": "ping"})
        for peer in self.peers:
            if (peer.connection is None and True not in peer.candidates
                    and now >= peer.retry_at):
                self._dial(peer, now)

    def broadcast(self, text: str) -> tuple[list[str], list[str]]:
        """Queue only for committed sessions. Success is NOT a display receipt."""
        queued: list[str] = []
        unavailable: list[str] = []
        for peer in self.peers:
            conn = peer.connection
            if conn is not None and conn.ready and self._queue(
                    conn, {"type": "message", "text": text}):
                queued.append(peer.config.label)
            else:
                unavailable.append(peer.config.label)
        return queued, unavailable
