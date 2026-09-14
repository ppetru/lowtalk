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
                  [PeerConfig('parent', int(sys.argv[2]), ('127.0.0.1',))],
                  '127.0.0.1', ui.event)
ui.network = network
try:
    curses.wrapper(ui.run)
finally:
    network.close()
"""


class TerminalTests(unittest.TestCase):
    def test_editing_truncation_and_resize_during_message_receipt(self):
        with socket.socket() as first, socket.socket() as second:
            first.bind(("127.0.0.1", 0))
            second.bind(("127.0.0.1", 0))
            child_port = first.getsockname()[1]
            parent_port = second.getsockname()[1]
        events = []
        network = Network("parent", parent_port,
                          [PeerConfig("child", child_port, ("127.0.0.1",))],
                          "127.0.0.1", lambda *event: events.append(event))
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        process = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(child_port), str(parent_port)],
            cwd=ROOT, stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(slave)
        os.set_blocking(master, False)
        output = bytearray()

        def pump_until(predicate, timeout=5):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                network.tick()
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
            pump_until(lambda: network.peers[0].status == "online")
            # Put the cursor in the middle, then receive a message.
            os.write(master, b"helo\x02")
            pump_until(lambda: b"helo" in output)
            network.broadcast("incoming while editing")
            pump_until(lambda: b"incoming while editing" in output)
            os.write(master, b"l\n")
            pump_until(lambda: any(text == "hello" for _, text in events))
            self.assertEqual(sum(text == "hello" for _, text in events), 1)
            # Exercise the actual per-key limit and its visible draft warning.
            os.write(master, b"x" * 4001)
            pump_until(lambda: b"Input truncated" in output)
            os.write(master, b"\n")
            pump_until(lambda: any(text == "x" * 4000 for _, text in events))
            self.assertEqual(sum(text == "x" * 4000 for _, text in events), 1)
            # Force a small layout and restore it. Ctrl-L requests a repaint.
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 4, 18, 0, 0))
            process.send_signal(signal.SIGWINCH)
            os.write(master, b"\x0c")
            pump_until(lambda: b"Resize terminal" in output)
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            process.send_signal(signal.SIGWINCH)
            os.write(master, b"\x0c/quit\n")
            pump_until(lambda: process.poll() is not None)
            self.assertEqual(process.wait(timeout=2), 0)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            network.close()
            os.close(master)


if __name__ == "__main__":
    unittest.main()
