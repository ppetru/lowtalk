"""Nonblocking TCP mesh, driven by tick() on the UI thread.

No worker threads touch curses. Each tick does bounded socket work so a busy or
slow peer cannot monopolize the input loop. Events are synchronous callbacks.
All deadlines use monotonic time; wall-clock adjustments cannot alter them.

Online means a valid hello, not Tailscale device presence. Any received bytes
refresh the inactivity deadline. A ping gets a pong; pongs get no reply. Routine
connection changes update status without adding events to conversation history.
There is no offline queue, reconnect replay, or application delivery receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import random
import selectors
import socket
import time
from typing import Callable

from chat_config import PeerConfig
from chat_protocol import FrameReader, ProtocolError, encode

CONNECT_TIMEOUT = 5.0
HEARTBEAT_INTERVAL = 10.0
DEAD_TIMEOUT = 30.0
MAX_PENDING = 128 * 1024


@dataclass
class Peer:
    config: PeerConfig
    connection: "Connection | None" = None
    nick: str = ""
    retry_at: float = 0.0
    backoff: float = 1.0
    address_index: int = 0
    last_error: str = ""

    @property
    def status(self) -> str:
        if self.connection is None:
            return "offline"
        return "online" if self.connection.ready else "connecting"


@dataclass
class Connection:
    sock: socket.socket
    peer: Peer
    address: str
    outgoing: bool
    created: float
    connecting: bool = False
    ready: bool = False
    reader: FrameReader = field(default_factory=FrameReader)
    output: bytearray = field(default_factory=bytearray)
    last_received: float = 0.0
    last_ping: float = 0.0


class Network:
    def __init__(
        self, nick: str, port: int, peers: list[PeerConfig], bind_ip: str,
        on_event: Callable[[str, str], None],
    ) -> None:
        self.nick = nick
        self.port = port
        self.bind_ip = bind_ip
        self.on_event = on_event
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
        for sock in list(self.connections):
            self.selector.unregister(sock)
            sock.close()
        self.connections.clear()
        self.listener.close()
        self.selector.close()

    def _interest(self, conn: Connection) -> None:
        events = selectors.EVENT_READ
        if conn.connecting or conn.output:
            events |= selectors.EVENT_WRITE
        self.selector.modify(conn.sock, events, conn)

    def _queue(self, conn: Connection, message: dict) -> bool:
        data = encode(message)
        if len(conn.output) + len(data) > MAX_PENDING:
            self._drop(conn, "outgoing buffer full; pending messages may be lost", warn=True)
            return False
        conn.output.extend(data)
        self._interest(conn)
        return True

    def _register(self, sock: socket.socket, peer: Peer, address: str,
                  outgoing: bool, connecting: bool = False) -> None:
        # Both sides may dial. Only the lexicographically smaller listening
        # endpoint's outbound connection survives, so there is no split-brain
        # choice of two different sockets. TCP source ports are ephemeral.
        local_endpoint = (sock.getsockname()[0], self.port)
        remote_endpoint = (address, peer.config.port)
        want_outgoing = local_endpoint < remote_endpoint
        if local_endpoint == remote_endpoint or outgoing != want_outgoing:
            sock.close()
            return
        if peer.connection is not None:
            sock.close()
            return
        now = time.monotonic()
        conn = Connection(
            sock=sock, peer=peer, address=address, outgoing=outgoing,
            created=now, connecting=connecting, last_received=now, last_ping=now,
        )
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.selector.register(sock, selectors.EVENT_READ, conn)
        self.connections[sock] = conn
        peer.connection = conn
        self._queue(conn, {"type": "hello", "version": 1,
                           "nick": self.nick, "port": self.port})

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
            peer = self.allowed.get(address)
            if peer is None:
                # Do not even send a hello to an unlisted source address.
                sock.close()
                continue
            try:
                self._register(sock, peer, address, False)
            except OSError:
                sock.close()

    def _drop(self, conn: Connection, reason: str, *, warn: bool = False) -> None:
        if conn.sock not in self.connections:
            return
        self.selector.unregister(conn.sock)
        del self.connections[conn.sock]
        conn.sock.close()
        peer = conn.peer
        peer.connection = None
        peer.last_error = reason
        now = time.monotonic()
        self._retry(peer, now)
        if conn.ready:
            if conn.output:
                self.on_event("!", f"{peer.config.label}: connection lost with pending data; "
                              "some messages may not have arrived")
            if warn:
                self.on_event("!", f"{peer.config.label}: {reason}")

    def _message(self, conn: Connection, message: dict) -> None:
        kind = message["type"]
        if not conn.ready:
            if kind != "hello" or message["port"] != conn.peer.config.port:
                raise ProtocolError("expected hello with configured listening port")
            conn.ready = True
            peer = conn.peer
            peer.nick = message["nick"]
            peer.last_error = ""
            peer.backoff = 1.0
        elif kind == "hello":
            raise ProtocolError("duplicate hello")
        elif kind == "message":
            self.on_event(f"{conn.peer.nick} @ {conn.address}", message["text"])
        elif kind == "ping":
            self._queue(conn, {"type": "pong"})

    def _service(self, conn: Connection, mask: int, now: float) -> None:
        try:
            if conn.connecting:
                error = conn.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if error:
                    raise OSError(error, "connect failed")
                conn.connecting = False
            if mask & selectors.EVENT_READ:
                data = conn.sock.recv(16_384)
                if not data:
                    self._drop(conn, "connection closed")
                    return
                conn.last_received = now
                for message in conn.reader.feed(data):
                    self._message(conn, message)
                    if conn.sock not in self.connections:
                        return
            if mask & selectors.EVENT_WRITE and conn.output:
                sent = conn.sock.send(conn.output)
                del conn.output[:sent]
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
                self._service(key.data, mask, now)
        for conn in list(self.connections.values()):
            if not conn.ready and now - conn.created > CONNECT_TIMEOUT:
                self._drop(conn, "handshake timed out")
            elif now - conn.last_received > DEAD_TIMEOUT:
                self._drop(conn, "heartbeat timed out")
            elif conn.ready and now - conn.last_ping >= HEARTBEAT_INTERVAL:
                conn.last_ping = now
                self._queue(conn, {"type": "ping"})
        for peer in self.peers:
            if peer.connection is None and now >= peer.retry_at:
                self._dial(peer, now)

    def broadcast(self, text: str) -> tuple[list[str], list[str]]:
        """Queue only for live sessions. Success is NOT a display receipt."""
        queued, unavailable = [], []
        for peer in self.peers:
            conn = peer.connection
            if conn is not None and conn.ready and self._queue(
                    conn, {"type": "message", "text": text}):
                queued.append(peer.config.label)
            else:
                unavailable.append(peer.config.label)
        return queued, unavailable
