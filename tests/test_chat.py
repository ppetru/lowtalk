import errno
import json
from pathlib import Path
import select
import selectors
import socket
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from chat_config import PeerConfig, load_peers, parse_port, tailscale_ip
from chat_network import CONNECT_TIMEOUT, Network, MAX_PENDING, Phase
from chat_protocol import (
    FrameReader, MAX_FRAME, MAX_MESSAGE, ProtocolError, clean_text, decode, encode,
)
from chat_ui import ChatUI, Editor, cell_width, wrap


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def two_ports():
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        return sorted((first.getsockname()[1], second.getsockname()[1]))


def peer(port, address="127.0.0.1"):
    return PeerConfig(address, port, (address,))


def hello(port, nick="other", instance="2" * 32):
    return {"type": "hello", "version": 2, "nick": nick, "port": port,
            "instance": instance}


def pump(networks, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for network in networks:
            network.tick()
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("network condition did not become true")


class Screen:
    def __init__(self, size=(10, 50)):
        self.rows = {}
        self.size = size

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.rows.clear()

    def addstr(self, row, column, text, attr):
        self.rows[row] = text

    def move(self, row, column):
        self.cursor = (row, column)

    def refresh(self):
        pass


class ConfigTests(unittest.TestCase):
    def test_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "peers"
            path.write_text("# friends\n127.0.0.1\n\n127.0.0.2 8888 # other\n")
            result = load_peers(path)
            self.assertEqual([p.port for p in result], [7777, 8888])
            self.assertEqual(result[1].addresses, ("127.0.0.2",))
            for content in ("", "127.0.0.1 nope", "127.0.0.1 0", "a b c",
                            "127.0.0.1\n127.0.0.1 8888"):
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    load_peers(path)

    def test_dns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "peers"
            path.write_text("friend 8888")
            with patch("chat_config.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.2", 8888))
            ]):
                self.assertEqual(load_peers(path)[0].addresses, ("100.64.0.2",))

    def test_ports(self):
        self.assertEqual(parse_port("65535"), 65535)
        for value in ("-1", "0", "65536", "abc"):
            with self.assertRaises(ValueError):
                parse_port(value)

    def test_discovery(self):
        with patch("chat_config.subprocess.run") as run:
            run.return_value.stdout = "100.64.1.2\n"
            self.assertEqual(tailscale_ip(), "100.64.1.2")
            run.return_value.stdout = "192.168.1.2\n"
            self.assertIsNone(tailscale_ip())
            run.side_effect = FileNotFoundError
            self.assertIsNone(tailscale_ip())


class ProtocolTests(unittest.TestCase):
    def test_fragmentation_and_coalescing(self):
        frame = encode({"type": "message", "text": "héllo 世界"})
        reader = FrameReader()
        self.assertEqual(list(reader.feed(frame[:8])), [])
        self.assertEqual(list(reader.feed(frame[8:] + frame)),
                         [{"type": "message", "text": "héllo 世界"}] * 2)
        self.assertEqual(reader.buffer, b"")

    def test_bad_frames(self):
        for frame in (b"not json", b"[]", b"\xff", b'{"type":"unknown"}',
                      '{"type":"ping"}'.encode("utf-16"),
                      b'{"type":"hello","version":true,"nick":"x","port":7777}',
                      b'{"type":"message","text":"\\u001b[2J"}',
                      b'{"type":"hello","version":2,"nick":"x","port":true}',
                      b'{"type":"message","text":"\\ud800"}',
                      b'{"type":"message","text":""}'):
            with self.subTest(frame=frame), self.assertRaises(ProtocolError):
                decode(frame)
        with self.assertRaises(ProtocolError):
            list(FrameReader().feed(b"x" * (MAX_FRAME + 1)))
        with self.assertRaises(ProtocolError):
            list(FrameReader().feed(b"x" * (MAX_FRAME + 1) + b"\n"))
        self.assertEqual(clean_text("a\x1bb\u202ec"), "abc")

    def test_hello_requires_supported_version_and_process_instance(self):
        for version in (1, 3):
            frame = hello(7777)
            frame["version"] = version
            with self.subTest(version=version), self.assertRaisesRegex(
                    ProtocolError, f"unsupported protocol version {version}; expected 2"):
                decode(json.dumps(frame).encode())
        for instance in (None, "", "a" * 31, "a" * 33, "A" * 32, "g" * 32, 1):
            frame = hello(7777)
            frame["instance"] = instance
            with self.subTest(instance=instance), self.assertRaises(ProtocolError):
                decode(json.dumps(frame).encode())
        frame = hello(7777)
        del frame["instance"]
        with self.assertRaises(ProtocolError):
            decode(json.dumps(frame).encode())

    def test_extra_fields_are_ignored_for_all_frame_types(self):
        for frame in (hello(7777), {"type": "message", "text": "hello"},
                      *({"type": kind} for kind in ("select", "accept", "ready", "ping", "pong"))):
            with self.subTest(frame=frame):
                extra = dict(frame, ignored={"anything": True})
                self.assertEqual(decode(json.dumps(extra).encode()), frame)

    def test_unicode_policy_is_not_the_runtime_character_database(self):
        # New emoji, private use, and unassigned scalars must survive even on a
        # Python whose Unicode database does not know how to display them.
        for text in ("\U0001fae8", "\U0001fae9", "\ue000", "\u0378"):
            for message in ({"type": "message", "text": text},
                            hello(7777, nick=text)):
                with self.subTest(message=message):
                    self.assertEqual(decode(encode(message)), message)
        for text in ("\x00", "\x1b", "\x7f", "\x9b", "\u202e", "\u200d",
                     "\u0890", "\U0001343f", "\U000e0020", "\ud800"):
            with self.subTest(text=repr(text)), self.assertRaises(ProtocolError):
                # json.dumps must escape surrogates, which encode() cannot encode.
                decode(json.dumps({"type": "message", "text": "a" + text}).encode())

    def test_valid_prefix_survives_invalid_suffix_at_every_split(self):
        message = {"type": "message", "text": "keep this"}
        payload = encode(message) + b"bad\n"
        for split in range(len(payload) + 1):
            reader = FrameReader()
            received = []
            with self.subTest(split=split), self.assertRaises(ProtocolError):
                for chunk in (payload[:split], payload[split:]):
                    received.extend(reader.feed(chunk))
            self.assertEqual(received, [message])
        for suffix in (b"x" * (MAX_FRAME + 1), b"x" * (MAX_FRAME + 1) + b"\n"):
            received = []
            with self.assertRaises(ProtocolError):
                received.extend(FrameReader().feed(encode(message) + suffix))
            self.assertEqual(received, [message])

    def test_maximum_message_fits_wire_limit(self):
        for char in ("x", "\\", "\U0001fae9"):
            message = {"type": "message", "text": char * MAX_MESSAGE}
            frame = encode(message)
            self.assertLessEqual(len(frame) - 1, MAX_FRAME)
            self.assertEqual(list(FrameReader().feed(frame)), [message])


class EditorTests(unittest.TestCase):
    def test_editing(self):
        editor = Editor()
        editor.insert("hello world")
        editor.key("\x17")
        self.assertEqual(editor.text, "hello ")
        editor.key("\x19")
        editor.key("HOME")
        editor.key("DELETE")
        editor.key("END")
        editor.key("BACKSPACE")
        self.assertEqual(editor.text, "ello worl")
        editor.key("\x01")
        editor.key("\x06")
        editor.key("\x0b")
        self.assertEqual(editor.text, "e")
        editor.key("\x19")
        editor.key("\x15")
        self.assertEqual(editor.text, "")
        editor.key("\x19")
        self.assertEqual(editor.take(), "ello worl")
        self.assertEqual(editor.cursor, 0)

    def test_receive_preserves_draft(self):
        ui = ChatUI("me")
        ui.editor.insert("unfinished")
        ui.editor.key("LEFT")
        ui.event("friend", "incoming")
        self.assertEqual((ui.editor.text, ui.editor.cursor), ("unfinished", 9))
        self.assertRegex(ui.messages[-1][1], r"^\d\d:\d\d \[friend\] incoming$")

    def test_rejected_escaped_draft_retries_only_on_enter_and_partial_send_clears(self):
        ui = ChatUI("me")
        ui.network = SimpleNamespace(
            peers=[],
            broadcast=Mock(side_effect=[([], ["friend"]), (["friend"], ["other"])]),
        )
        ui.editor.insert("//hello")
        ui.editor.key("LEFT")
        ui.editor.key("LEFT")
        self.assertTrue(ui._send())
        self.assertEqual((ui.editor.text, ui.editor.cursor), ("//hello", 5))
        self.assertTrue(any("Not sent" in text for _, text in ui.messages))
        self.assertFalse(any("[me (you)]" in text for _, text in ui.messages))
        ui.event("friend", "back online")
        ui.draw(Screen())
        self.assertEqual(ui.network.broadcast.call_count, 1)

        self.assertTrue(ui._send())
        self.assertEqual((ui.editor.text, ui.editor.cursor), ("", 0))
        self.assertEqual(ui.network.broadcast.call_count, 2)
        self.assertTrue(all(call.args == ("/hello",)
                            for call in ui.network.broadcast.call_args_list))
        self.assertEqual(sum("[me (you)] /hello" in text for _, text in ui.messages), 1)
        self.assertTrue(any("Not queued for: other" in text for _, text in ui.messages))
        ui._send()
        ui.draw(Screen())
        self.assertEqual(ui.network.broadcast.call_count, 2)

    def test_rejected_truncated_draft_keeps_cursor_and_feedback_until_queued(self):
        ui = ChatUI("me")
        ui.network = SimpleNamespace(
            peers=[], broadcast=Mock(side_effect=[([], ["friend"]), (["friend"], [])]),
        )
        ui.editor.insert("x" * (MAX_MESSAGE + 1))
        ui.editor.key("HOME")
        ui.editor.key("RIGHT")
        ui._send()
        self.assertEqual((ui.editor.text, ui.editor.cursor), ("x" * MAX_MESSAGE, 1))
        self.assertTrue(ui.editor.truncated)
        screen = Screen()
        ui.draw(screen)
        self.assertIn("Input truncated", screen.rows[screen.size[0] - 2])
        ui.editor.key("DELETE")
        self.assertTrue(ui.editor.truncated)
        ui._send()
        self.assertEqual(ui.network.broadcast.call_args.args, ("x" * (MAX_MESSAGE - 1),))
        self.assertEqual((ui.editor.text, ui.editor.cursor, ui.editor.truncated), ("", 0, False))
        self.assertEqual(sum("Input truncated" in text for _, text in ui.messages), 1)

    def test_session_boundary_appends_without_rewriting_history_or_moving_anchor(self):
        ui = ChatUI("me")
        ui.network = SimpleNamespace(peers=[])
        screen = Screen((10, 100))
        for number in range(30):
            ui.event("friend", f"message {number}")
        ui.editor.insert("unfinished")
        ui.editor.key("LEFT")
        ui.draw(screen)
        ui._scroll(-1)
        ui.draw(screen)
        anchor, first_line = ui.anchor, screen.rows[1]
        history = list(ui.messages)
        boundary = "friend has a new Lowtalk session; previous scrollback was cleared."
        ui.event("---", boundary)
        ui.draw(screen)
        self.assertEqual(list(ui.messages)[:-1], history)
        self.assertRegex(ui.messages[-1][1], r"^\d\d:\d\d --- " + boundary + r" ---$")
        self.assertEqual(ui.anchor, anchor)
        self.assertEqual(screen.rows[1], first_line)
        self.assertEqual(ui.unread, 1)
        screen.size = (12, 80)
        ui.draw(screen)
        self.assertEqual((ui.editor.text, ui.editor.cursor), ("unfinished", 9))
        for _ in range(10):
            ui._scroll(1)
        ui.draw(screen)
        self.assertIsNone(ui.anchor)
        self.assertEqual(ui.unread, 0)
        self.assertTrue(any(boundary in row for row in screen.rows.values()))

    def test_unicode_and_limits(self):
        self.assertEqual(cell_width("a界e\u0301"), 4)
        self.assertEqual(wrap("a界b", 3), ["a界", "b"])
        editor = Editor()
        editor.insert("x" * 5000)
        self.assertEqual(len(editor.text), 4000)

    def test_scrolling_stays_anchored_and_draft_survives_redraw(self):
        ui = ChatUI("me")
        ui.network = SimpleNamespace(peers=[])
        screen = Screen()
        for number in range(30):
            ui.event("friend", f"message {number}")
        ui.editor.insert("draft")
        ui.editor.key("LEFT")
        ui.draw(screen)
        ui._scroll(-1)
        ui.draw(screen)
        first_line = screen.rows[1]
        ui.event("friend", "new arrival")
        ui.draw(screen)
        self.assertEqual(screen.rows[1], first_line)
        self.assertEqual(ui.unread, 1)
        self.assertEqual((ui.editor.text, ui.editor.cursor), ("draft", 4))
        screen.size = (12, 30)
        ui.draw(screen)
        self.assertEqual((ui.editor.text, ui.editor.cursor), ("draft", 4))
        for _ in range(10):
            ui._scroll(1)
            ui.draw(screen)
        self.assertIsNone(ui.anchor)
        self.assertEqual(ui.unread, 0)

    def test_truncation_is_reported_in_draft_and_after_immediate_send(self):
        ui = ChatUI("me")
        ui.network = SimpleNamespace(peers=[], broadcast=Mock(return_value=(["friend"], [])))
        screen = Screen()
        ui.editor.insert("x" * MAX_MESSAGE)
        self.assertFalse(ui.editor.truncated)
        ui.editor.key("y")
        self.assertTrue(ui.editor.truncated)
        ui.draw(screen)
        self.assertIn("Input truncated", screen.rows[screen.size[0] - 2])
        ui._send()
        ui.network.broadcast.assert_called_once_with("x" * MAX_MESSAGE)
        self.assertFalse(ui.editor.truncated)
        ui.draw(screen)
        self.assertNotIn("Input truncated", screen.rows[screen.size[0] - 2])
        # No redraw between an overlong paste and Enter: feedback still persists.
        ui.editor.insert("y" * (MAX_MESSAGE + 1))
        ui._send()
        ui.draw(screen)
        self.assertTrue(any("Input truncated" in row for row in screen.rows.values()))
        self.assertEqual(sum("Input truncated" in text for _, text in ui.messages), 2)
        ui.editor.insert("normal")
        ui._send()
        self.assertEqual(sum("Input truncated" in text for _, text in ui.messages), 2)

    def test_truncation_on_yank_stays_flagged_until_submission(self):
        editor = Editor(text="x" * MAX_MESSAGE, cursor=MAX_MESSAGE, killed="word")
        editor.key("\x19")
        self.assertTrue(editor.truncated)
        editor.key("BACKSPACE")
        self.assertTrue(editor.truncated)
        editor.take()
        self.assertFalse(editor.truncated)
        editor.key("\U0001fae9")
        editor.key("\x1b")
        self.assertEqual(editor.text, "\U0001fae9")

    def test_multiple_page_keys_before_redraw(self):
        ui = ChatUI("me")
        ui.network = SimpleNamespace(peers=[])
        screen = Screen()
        for i in range(100):
            ui.event("friend", str(i))
        ui.draw(screen)
        bottom = ui.view_start[0]
        for _ in range(3):
            ui._scroll(-1)
        self.assertEqual(ui.anchor, (bottom - 3 * ui.page_height, 0))
        for _ in range(2):
            ui._scroll(1)
        ui.draw(screen)
        self.assertEqual(ui.view_start, (bottom - ui.page_height, 0))
        ui._scroll(1)
        self.assertIsNone(ui.anchor)

    def test_lazy_layout_matches_full_wrapping_during_navigation_and_resize(self):
        ui = ChatUI("me")
        ui.network = SimpleNamespace(peers=[])
        screen = Screen()
        for i in range(80):
            ui.event("friend", "界e\u0301 " * (i % 12 + 1))
        for width in (50, 20, 70):
            screen.size = (10, width)
            ui.draw(screen)
            full = [((seq, index), line) for seq, text in ui.messages
                    for index, line in enumerate(wrap(text, width - 1))]
            bottom = max(0, len(full) - ui.page_height)
            start = next(i for i, (position, _) in enumerate(full) if position == ui.view_start)
            for direction in [-1] * 90 + [1] * 90:
                start = min(bottom, max(0, start + direction * ui.page_height))
                ui._scroll(direction)
                ui.draw(screen)
                self.assertEqual(ui.rendered, full[start:start + ui.page_height])
                self.assertLessEqual(len(ui.wrapped), 64)

    def test_full_history_resize_wraps_only_visible_messages(self):
        ui = ChatUI("me")
        ui.network = SimpleNamespace(peers=[])
        screen = Screen((24, 80))
        for _ in range(1000):
            ui.event("friend", "界" * MAX_MESSAGE)
        # Count work instead of imposing a machine-dependent timing threshold.
        with patch("chat_ui.wrap", wraps=wrap) as wrapping:
            ui.draw(screen)
            screen.size = (24, 20)
            ui.draw(screen)
        message_wraps = [call for call in wrapping.call_args_list
                         if len(call.args[0]) >= MAX_MESSAGE]
        self.assertEqual(len(message_wraps), 2)
        self.assertLessEqual(len(ui.rendered), ui.page_height)
        self.assertEqual(len(ui.wrapped), 1)
        ui.anchor = (0, 0)
        ui.draw(screen)
        ui.event("friend", "new")
        ui.draw(screen)
        self.assertEqual(ui.view_start, (1, 0))
        self.assertNotIn(0, ui.wrapped)

    def test_scrollback_bound(self):
        ui = ChatUI("me")
        for i in range(1002):
            ui.event("x", str(i))
        self.assertEqual(len(ui.messages), 1000)
        self.assertEqual(ui.messages[0][0], 2)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.networks = []

    def tearDown(self):
        for network in self.networks:
            network.close()

    def create(self, nick, port, peers, events, instance="1" * 32):
        with patch("chat_network.secrets.token_hex", return_value=instance):
            network = Network(nick, port, peers, "127.0.0.1", lambda *e: events.append(e))
        self.networks.append(network)
        return network

    def pair(self, connect=True, instances=("1" * 32, "2" * 32)):
        a_port, b_port = two_ports()
        a_events, b_events = [], []
        a = self.create("alice", a_port, [peer(b_port)], a_events, instances[0])
        b = self.create("bob", b_port, [peer(a_port)], b_events, instances[1])
        if connect:
            pump([a, b], lambda: a.peers[0].status == b.peers[0].status == "online")
        return a, b, a_events, b_events

    def test_smaller_instance_coordinates_even_on_larger_listening_endpoint(self):
        a, b, events, b_events = self.pair(connect=False, instances=("2" * 32, "1" * 32))
        for network in (a, b):
            network._dial(network.peers[0], time.monotonic())
        for network in (a, b):
            network._accept()
        chosen = b.peers[0].candidates[True]
        for network in (a, b):
            for conn in list(network.connections.values()):
                network._service(conn, selectors.EVENT_WRITE, time.monotonic())
        self.read_socket(b, chosen)
        self.assertEqual(chosen.phase, Phase.SELECTED)
        self.assertIs(b.peers[0].connection, chosen)
        pump([a, b], lambda: a.peers[0].status == b.peers[0].status == "online")
        self.assertFalse(a.peers[0].connection.outgoing)
        self.assertTrue(b.peers[0].connection.outgoing)
        self.assertEqual(events, [])
        self.assertEqual(b_events, [])

    def read_socket(self, network, conn):
        self.assertEqual(select.select([conn.sock], [], [], 1)[0], [conn.sock])
        network._service(conn, selectors.EVENT_READ, time.monotonic())

    def reject_late_socket(self, network):
        chosen = network.peers[0].connection
        retry_at = network.peers[0].retry_at
        with socket.create_connection(("127.0.0.1", network.port)) as duplicate:
            duplicate.settimeout(1)
            network._accept()
            self.assertEqual(duplicate.recv(1000), b"")
        self.assertIs(network.peers[0].connection, chosen)
        self.assertEqual(network.peers[0].retry_at, retry_at)

    def test_either_direction_and_startup_order_can_establish_a_session(self):
        for alice_dials in (False, True):
            for alice_ticks_first in (False, True):
                with self.subTest(alice_dials=alice_dials, alice_ticks_first=alice_ticks_first):
                    a, b, a_events, b_events = self.pair(connect=False)
                    for network in (a, b):
                        network.peers[0].retry_at = float("inf")
                    initiator = a if alice_dials else b
                    initiator._dial(initiator.peers[0], time.monotonic())
                    order = [a, b] if alice_ticks_first else [b, a]
                    pump(order, lambda: a.peers[0].status == b.peers[0].status == "online")
                    self.assertEqual(a.peers[0].connection.outgoing, alice_dials)
                    self.assertEqual(b.peers[0].connection.outgoing, not alice_dials)
                    a.broadcast("from alice")
                    b.broadcast("from bob")
                    pump(order, lambda: bool(a_events) and bool(b_events))
                    self.assertEqual([text for _, text in a_events], ["from bob"])
                    self.assertEqual([text for _, text in b_events], ["from alice"])

    def test_first_valid_candidate_wins_and_late_selector_events_are_safe(self):
        for outgoing in (False, True):
            with self.subTest(first_valid_outgoing=outgoing):
                a, b, a_events, b_events = self.pair(connect=False)
                for network in (a, b):
                    network._dial(network.peers[0], time.monotonic())
                for network in (a, b):
                    network._accept()
                    self.assertEqual(set(network.peers[0].candidates), {False, True})
                chosen = a.peers[0].candidates[outgoing]
                loser = a.peers[0].candidates[not outgoing]
                follower = b.peers[0].candidates[not outgoing]
                for network in (a, b):
                    for conn in list(network.connections.values()):
                        network._service(conn, selectors.EVENT_WRITE, time.monotonic())
                self.read_socket(a, chosen)
                self.assertIs(a.peers[0].connection, chosen)
                self.assertEqual(chosen.phase, Phase.SELECTED)
                self.assertEqual(len(a.connections), 1)
                a._service(loser, selectors.EVENT_READ | selectors.EVENT_WRITE, time.monotonic())
                self.assertIs(a.peers[0].connection, chosen)
                self.assertEqual(a.broadcast("too early"), ([], [a.peers[0].config.label]))
                self.reject_late_socket(a)
                self.read_socket(b, follower)
                self.assertEqual(follower.phase, Phase.CANDIDATE)
                a._service(chosen, selectors.EVENT_WRITE, time.monotonic())
                self.read_socket(b, follower)
                self.assertEqual(follower.phase, Phase.ACCEPTED)
                self.assertEqual(b.broadcast("too early"), ([], [b.peers[0].config.label]))
                self.reject_late_socket(b)
                b._service(follower, selectors.EVENT_WRITE, time.monotonic())
                self.read_socket(a, chosen)
                self.assertTrue(chosen.ready)
                self.assertFalse(follower.ready)
                self.reject_late_socket(a)
                a.broadcast("after commit")
                self.assertEqual(list(FrameReader().feed(bytes(chosen.output))), [
                    {"type": "ready"}, {"type": "message", "text": "after commit"}])
                a._service(chosen, selectors.EVENT_WRITE, time.monotonic())
                self.read_socket(b, follower)
                self.assertTrue(follower.ready)
                self.assertEqual([text for _, text in b_events], ["after commit"])
                self.assertEqual(a_events, [])
                self.assertEqual(len(b.connections), 1)
                self.reject_late_socket(b)

    def test_wrong_handshake_order_cannot_deliver_application_data(self):
        for instance, prefix, expected in (
                ("1" * 32, [], "configured listening port"),
                ("3" * 32, [hello(7777)], "expected select"),
                ("1" * 32, [hello(7777)], "expected accept"),
                ("3" * 32, [hello(7777), {"type": "select"}], "expected ready")):
            with self.subTest(instance=instance, expected=expected):
                events = []
                network = self.create("me", free_port(), [peer(7777)], events, instance)
                network.peers[0].retry_at = float("inf")
                with socket.create_connection(("127.0.0.1", network.port)) as sock:
                    network._accept()
                    sock.sendall(b"".join(encode(frame) for frame in prefix)
                                 + encode({"type": "message", "text": "too early"}))
                    pump([network], lambda: not network.connections)
                self.assertIn(expected, network.peers[0].last_error)
                self.assertIsNone(network.peers[0].last_instance)
                self.assertFalse(any(text == "too early" for _, text in events))
                self.assertEqual(events, [])

    def test_reconnect_same_instance_retains_history_identity_without_boundary(self):
        a, b, events, b_events = self.pair()
        previous = a.peers[0].last_instance
        a._drop(a.peers[0].connection, "connection closed")
        b._drop(b.peers[0].connection, "connection closed")
        self.assertEqual(a.peers[0].last_instance, previous)
        a.peers[0].retry_at = b.peers[0].retry_at = 0
        pump([a, b], lambda: a.peers[0].status == b.peers[0].status == "online")
        b.broadcast("same session")
        pump([a, b], lambda: bool(events))
        self.assertEqual([text for _, text in events], ["same session"])
        self.assertEqual(a.peers[0].last_instance, previous)
        self.assertEqual(b_events, [])

    def test_uncommitted_and_rejected_candidates_do_not_change_history_identity(self):
        a, b, events, _ = self.pair()
        previous = a.peers[0].last_instance
        self.reject_late_socket(a)
        self.assertEqual(a.peers[0].last_instance, previous)
        a._drop(a.peers[0].connection, "connection closed")
        b._drop(b.peers[0].connection, "connection closed")
        a.peers[0].retry_at = b.peers[0].retry_at = float("inf")
        for instance, suffix, phase in (
                ("3" * 32, [], Phase.SELECTED),
                ("0" * 32, [{"type": "select"}], Phase.ACCEPTED)):
            with self.subTest(phase=phase):
                with socket.create_connection(("127.0.0.1", a.port)) as sock:
                    a._accept()
                    conn = a.peers[0].candidates[False]
                    sock.sendall(encode(hello(b.port, instance=instance))
                                 + b"".join(encode(frame) for frame in suffix))
                    self.read_socket(a, conn)
                    self.assertEqual(conn.phase, phase)
                    self.assertEqual(a.peers[0].last_instance, previous)
                pump([a], lambda: not a.connections)
                a.peers[0].retry_at = float("inf")
                self.assertEqual(a.peers[0].last_instance, previous)
                self.assertEqual(events, [])
        a.peers[0].retry_at = b.peers[0].retry_at = 0
        pump([a, b], lambda: a.peers[0].status == b.peers[0].status == "online")
        self.assertEqual(events, [])

    def test_new_session_boundary_precedes_coalesced_first_message(self):
        a, b, events, _ = self.pair()
        b.close()
        self.networks.remove(b)
        pump([a], lambda: a.peers[0].status == "offline")
        self.assertEqual(a.peers[0].last_instance, "2" * 32)
        b = self.create("new bob", b.port, [peer(a.port)], [], "0" * 32)
        a.peers[0].retry_at = 0
        pump([a, b], lambda: b.peers[0].status == "online")
        self.assertEqual(a.peers[0].status, "connecting")
        b.broadcast("first in new session")
        self.assertEqual(list(FrameReader().feed(bytes(b.peers[0].connection.output))), [
            {"type": "ready"}, {"type": "message", "text": "first in new session"}])
        pump([a, b], lambda: any(text == "first in new session" for _, text in events))
        self.assertEqual(events, [
            ("---", "new bob has a new Lowtalk session; previous scrollback was cleared."),
            ("new bob @ 127.0.0.1", "first in new session")])
        self.assertEqual(a.peers[0].last_instance, b.instance)

    def test_retry_countdown_does_not_change_semantic_status(self):
        network = self.create("me", free_port(), [peer(free_port())], [])
        peer_state = network.peers[0]
        peer_state.retry_at = 103.1
        with patch("chat_network.time.monotonic", return_value=100):
            self.assertEqual(peer_state.status, "offline")
            self.assertEqual(peer_state.display_status, "offline (retry in 4s)")
        with patch("chat_network.time.monotonic", return_value=104):
            self.assertEqual(peer_state.display_status, "offline")

    def test_bidirectional_delivery_and_one_connection(self):
        a, b, a_events, b_events = self.pair()
        self.assertEqual(len(a.connections), 1)
        self.assertEqual(len(b.connections), 1)
        self.assertEqual(a.broadcast("hello")[1], [])
        b.broadcast("hi")
        pump(self.networks, lambda: any(e[1] == "hello" for e in b_events)
             and any(e[1] == "hi" for e in a_events))
        self.assertEqual(sum(e[1] == "hello" for e in b_events), 1)
        self.assertIn("127.0.0.1", b_events[-1][0])

    def test_reconnect_without_offline_replay(self):
        a, b, a_events, b_events = self.pair()
        b.close()
        self.networks.remove(b)
        pump([a], lambda: a.peers[0].status == "offline")
        self.assertEqual(a.broadcast("lost")[0], [])
        new_events = []
        b = self.create("bob", b.port, [peer(a.port)], new_events, "3" * 32)
        a.peers[0].retry_at = 0
        pump(self.networks, lambda: a.peers[0].status == b.peers[0].status == "online")
        a.broadcast("new")
        pump(self.networks, lambda: any(e[1] == "new" for e in new_events))
        self.assertFalse(any(e[1] == "lost" for e in new_events))
        self.assertEqual(a_events, [
            ("---", "bob has a new Lowtalk session; previous scrollback was cleared.")])
        self.assertEqual(a.peers[0].last_instance, b.instance)

    def test_unlisted_source_gets_no_hello(self):
        events = []
        network = self.create("me", free_port(), [peer(free_port(), "127.0.0.2")], events)
        with socket.create_connection(("127.0.0.1", network.port)) as sock:
            sock.settimeout(1)
            network.tick()
            self.assertEqual(sock.recv(1000), b"")
        self.assertEqual(events, [])

    def test_handshake_required_and_port_checked(self):
        low, high = two_ports()
        events = []
        network = self.create("me", high, [peer(low)], events)
        for frame in ({"type": "message", "text": "before hello"}, hello(high)):
            with socket.create_connection(("127.0.0.1", high)) as sock:
                sock.sendall(encode(frame))
                network._accept()
                pump([network], lambda: not network.connections)
                self.assertIn("configured listening port", network.peers[0].last_error)
        self.assertEqual(events, [])
        self.assertIsNone(network.peers[0].last_instance)

    def test_unsupported_version_is_actionable_before_readiness(self):
        remote_port = free_port()
        events = []
        network = self.create("me", free_port(), [peer(remote_port)], events)
        network.peers[0].retry_at = float("inf")
        frame = hello(remote_port)
        frame["version"] = 1
        for _ in range(3):
            with socket.create_connection(("127.0.0.1", network.port)) as sock:
                network._accept()
                sock.sendall(json.dumps(frame).encode() + b"\n")
                pump([network], lambda: not network.connections)
        self.assertEqual(network.peers[0].last_error,
                         "unsupported protocol version 1; expected 2")
        self.assertEqual(events, [])
        self.assertIsNone(network.peers[0].last_instance)

    def test_duplicate_direction_does_not_postpone_reconnect(self):
        low, high = two_ports()
        network = self.create("me", low, [peer(high)], [])
        network.peers[0].retry_at = float("inf")
        with socket.create_connection(("127.0.0.1", low)):
            network._accept()
            first = network.peers[0].candidates[False]
            with socket.create_connection(("127.0.0.1", low)) as duplicate:
                duplicate.settimeout(1)
                network._accept()
                self.assertEqual(duplicate.recv(1000), b"")
            self.assertEqual(network.peers[0].retry_at, float("inf"))
            self.assertEqual(network.peers[0].candidates, {False: first})
            self.assertEqual(network.peers[0].status, "connecting")

    def test_occupied_port(self):
        a, _, _, _ = self.pair()
        with self.assertRaises(OSError) as caught:
            Network("other", a.port, [peer(free_port())], "127.0.0.1", lambda *e: None)
        self.assertEqual(caught.exception.errno, errno.EADDRINUSE)

    def test_heartbeat_timeout_is_quiet(self):
        a, b, events, _ = self.pair()
        a.peers[0].connection.last_received = time.monotonic() - 31
        a.tick()
        self.assertEqual(a.peers[0].status, "offline")
        self.assertEqual(a.peers[0].last_error, "heartbeat timed out")
        self.assertEqual(events, [])
        a.peers[0].retry_at = 0
        pump(self.networks, lambda: a.peers[0].status == b.peers[0].status == "online")
        self.assertEqual(events, [])
        self.assertEqual(a.peers[0].last_error, "")

    def test_timeout_with_pending_data_warns(self):
        a, _, events, _ = self.pair()
        conn = a.peers[0].connection
        conn.output.extend(b"pending")
        a._drop(conn, "heartbeat timed out")
        self.assertEqual(a.peers[0].status, "offline")
        self.assertTrue(any("some messages may not have arrived" in text
                            for _, text in events))

    def test_valid_message_is_delivered_before_malformed_frame_disconnects(self):
        a, b, _, events = self.pair()
        conn = a.peers[0].connection
        conn.output.extend(encode({"type": "message", "text": "keep this"}) + b"bad\n")
        a._interest(conn)
        pump(self.networks, lambda: b.peers[0].status == "offline")
        self.assertEqual(sum(text == "keep this" for _, text in events), 1)
        self.assertIn("invalid JSON", b.peers[0].last_error)
        self.assertTrue(any("invalid JSON" in text for _, text in events))

    def test_handshake_deadline_is_absolute_even_when_valid_data_arrives(self):
        low, high = two_ports()
        events = []
        network = self.create("me", high, [peer(low)], events)
        network.peers[0].retry_at = float("inf")
        with socket.create_connection(("127.0.0.1", high)) as sock:
            network._accept()
            conn = network.peers[0].candidates[False]
            sock.sendall(b'{"type":')
            network._service(conn, selectors.EVENT_READ, conn.created + 1)
            self.assertEqual(conn.last_received, conn.created + 1)
            sock.sendall(encode(hello(low))[8:] + encode({"type": "accept"}))
            network._service(conn, selectors.EVENT_READ, conn.created + CONNECT_TIMEOUT)
        self.assertEqual(network.peers[0].status, "offline")
        self.assertEqual(network.peers[0].last_error, "handshake timed out")
        self.assertIsNone(network.peers[0].last_instance)
        self.assertEqual(events, [])

    def test_idle_heartbeats_over_three_simulated_days(self):
        a, b, a_events, b_events = self.pair()
        connections = [a.peers[0].connection, b.peers[0].connection]
        now = time.monotonic()
        # Advance only the clock; real loopback TCP still carries every ping and
        # pong. No sleeping for days and no messages to mask broken heartbeats.
        with patch("chat_network.time.monotonic", new=lambda: now):
            for _ in range(3 * 24 * 60 * 6):
                now += 10
                for network in (a, b, a, b):
                    network.tick()
                self.assertEqual([a.peers[0].connection, b.peers[0].connection], connections)
            for conn in connections:
                self.assertGreaterEqual(conn.last_received, now - 10)
                self.assertLess(len(conn.output), 100)
        self.assertEqual(a_events, [])
        self.assertEqual(b_events, [])

    def test_repeated_reconnect_after_day_long_outages(self):
        a, b, events, _ = self.pair()
        now = time.monotonic()
        with patch("chat_network.time.monotonic", new=lambda: now):
            for outage in range(3):
                b.close()
                self.networks.remove(b)
                a.tick()
                self.assertEqual(a.peers[0].status, "offline")
                self.assertEqual(a.broadcast("do not replay")[0], [])
                now += 86400
                a.tick()
                b_events = []
                b = self.create("bob", b.port, [peer(a.port)], b_events, str(outage + 3) * 32)
                for _ in range(400):
                    now += 0.25
                    a.tick()
                    b.tick()
                    if a.peers[0].status == b.peers[0].status == "online":
                        break
                self.assertEqual(a.peers[0].status, "online")
                self.assertEqual(b.peers[0].status, "online")
                for _ in range(12):
                    a.tick()
                    b.tick()
                a.broadcast(f"after outage {outage}")
                for _ in range(8):
                    a.tick()
                    b.tick()
                self.assertTrue(any(text == f"after outage {outage}" for _, text in b_events))
                self.assertFalse(any(text == "do not replay" for _, text in b_events))
                self.assertEqual(a.peers[0].backoff, 1)
        self.assertEqual(events, [
            ("---", "bob has a new Lowtalk session; previous scrollback was cleared.")] * 3)

    def test_backpressure_disconnects_instead_of_growing(self):
        a, _, events, _ = self.pair()
        conn = a.peers[0].connection
        conn.output = bytearray(b"x" * MAX_PENDING)
        self.assertEqual(a.broadcast("cannot queue")[0], [])
        self.assertEqual(a.peers[0].status, "offline")
        self.assertTrue(any("pending" in text for _, text in events))


if __name__ == "__main__":
    unittest.main()
