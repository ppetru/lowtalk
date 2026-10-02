"""Exercise real curses and TCP together, without Tailscale or a human terminal."""

import fcntl
import os
from pathlib import Path
import pty
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import unittest

from chat_config import PeerConfig
from chat_network import Network

ROOT = Path(__file__).resolve().parents[1]

CHILD = """
import curses
import sys
from chat_config import PeerConfig
from chat_network import Network
from chat_ui import ChatUI
ui = ChatUI('child')
network = Network('child', int(sys.argv[1]),
                  [PeerConfig('parent', int(sys.argv[2]), ('127.0.0.1',)),
                   PeerConfig('absent', int(sys.argv[3]), ('127.0.0.1',))],
                  '127.0.0.1', ui.event)
ui.network = network
try:
    curses.wrapper(ui.run)
finally:
    network.close()
"""


class TerminalTests(unittest.TestCase):
    def test_rejected_draft_partial_send_truncation_and_resize(self):
        with socket.socket() as first, socket.socket() as second, socket.socket() as third:
            first.bind(("127.0.0.1", 0))
            second.bind(("127.0.0.1", 0))
            third.bind(("127.0.0.1", 0))
            child_port = first.getsockname()[1]
            parent_port = second.getsockname()[1]
            absent_port = third.getsockname()[1]
        events = []
        absent_events = []
        network = None
        absent = None
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        process = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(child_port), str(parent_port), str(absent_port)],
            cwd=ROOT, stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(slave)
        os.set_blocking(master, False)
        output = bytearray()

        def pump_until(predicate, timeout=5):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if network is not None:
                    network.tick()
                if absent is not None:
                    absent.tick()
                try:
                    output.extend(os.read(master, 65536))
                except BlockingIOError:
                    pass
                except OSError:
                    pass  # PTY may report EIO just before the child is reaped.
                if predicate():
                    return
                if process.poll() is not None:
                    break
                time.sleep(0.01)
            self.fail(
                f"terminal condition failed; exit={process.poll()}, output={output[-3000:]!r}"
            )

        try:
            # No listener exists yet: Enter must reject without consuming the
            # escaped draft or its cursor, and connection recovery must not retry.
            pump_until(lambda: b"[child]" in output)
            os.write(master, b"//helo\x02\n")
            pump_until(lambda: b"Not sent" in output and b"Draft kept" in output)
            self.assertNotIn(b"[child (you)]", output)
            network = Network(
                "parent", parent_port,
                [PeerConfig("child", child_port, ("127.0.0.1",))],
                "127.0.0.1", lambda *event: events.append(event),
            )
            pump_until(lambda: network.peers[0].status == "online")
            network.broadcast("incoming while editing")
            pump_until(lambda: b"incoming while editing" in output)
            self.assertFalse(any(text == "/helo" for _, text in events))
            self.assertNotIn(b"new Lowtalk session", output)
            # A fresh parent process identity appends a history separator while
            # the rejected draft is still waiting at its original middle cursor.
            network.close()
            network = Network(
                "parent", parent_port,
                [PeerConfig("child", child_port, ("127.0.0.1",))],
                "127.0.0.1", lambda *event: events.append(event),
            )
            pump_until(lambda: network.peers[0].status == "online", timeout=10)
            pump_until(lambda: b"--- parent has a new Lowtalk session" in output, timeout=10)
            self.assertFalse(any(text == "/helo" for _, text in events))
            # The middle cursor survived rejection and receipt. This explicit
            # retry succeeds for parent only; absent must remain unavailable.
            os.write(master, b"l\n")
            pump_until(lambda: any(text == "/hello" for _, text in events))
            pump_until(lambda: b"Not queued for: absent" in output)
            self.assertEqual(sum(text == "/hello" for _, text in events), 1)
            absent = Network(
                "absent", absent_port,
                [PeerConfig("child", child_port, ("127.0.0.1",))],
                "127.0.0.1", lambda *event: absent_events.append(event),
            )
            pump_until(lambda: absent.peers[0].status == "online", timeout=10)
            self.assertFalse(any(text == "/hello" for _, text in absent_events))
            # Exercise the actual per-key limit and its visible draft warning.
            os.write(master, b"x" * 4001)
            pump_until(lambda: b"Input truncated" in output)
            os.write(master, b"\n")
            pump_until(lambda: any(text == "x" * 4000 for _, text in events)
                       and any(text == "x" * 4000 for _, text in absent_events))
            self.assertEqual(sum(text == "x" * 4000 for _, text in events), 1)
            self.assertEqual(sum(text == "x" * 4000 for _, text in absent_events), 1)
            self.assertEqual(sum(text == "/hello" for _, text in events), 1)
            # Force a small layout and restore it. Ctrl-L requests a repaint.
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 4, 18, 0, 0))
            process.send_signal(signal.SIGWINCH)
            os.write(master, b"\x0c")
            pump_until(lambda: b"Resize terminal" in output)
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            process.send_signal(signal.SIGWINCH)
            # A single key batch must flush the last message before /quit.
            os.write(master, b"\x0cgoodbye\n/quit\n")
            pump_until(lambda: process.poll() is not None)
            self.assertEqual(process.wait(timeout=2), 0)
            for _ in range(8):
                network.tick()
                absent.tick()
            self.assertEqual(sum(text == "goodbye" for _, text in events), 1)
            self.assertEqual(sum(text == "goodbye" for _, text in absent_events), 1)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if network is not None:
                network.close()
            if absent is not None:
                absent.close()
            os.close(master)


if __name__ == "__main__":
    unittest.main()
