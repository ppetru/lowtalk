"""Test binding policy without touching real Tailscale interfaces."""

from contextlib import ExitStack
import errno
import io
import unittest
from unittest.mock import Mock, patch

from chat_config import PeerConfig
import lowtalk


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("sys.argv", ["lowtalk.py", "me", "8888"]))
        self.stack.enter_context(patch("sys.stdin.isatty", return_value=True))
        self.stack.enter_context(patch("sys.stdout.isatty", return_value=True))
        self.stack.enter_context(patch(
            "lowtalk.load_peers",
            return_value=[PeerConfig("friend", 7777, ("100.64.0.2",))],
        ))
        self.discovery = self.stack.enter_context(
            patch("lowtalk.tailscale_ip", return_value="100.64.0.1")
        )
        self.factory = self.stack.enter_context(patch("lowtalk.Network"))
        self.wrapper = self.stack.enter_context(patch("curses.wrapper", return_value=[]))
        self.ui = self.stack.enter_context(patch("lowtalk.ChatUI")).return_value
        self.stderr = self.stack.enter_context(patch("sys.stderr", new_callable=io.StringIO))

    def test_prefer_tailscale_bind(self):
        self.factory.return_value.bind_ip = "100.64.0.1"
        self.assertEqual(lowtalk.main(), 0)
        self.assertEqual(self.factory.call_args.args[3], "100.64.0.1")
        self.assertEqual(self.factory.call_args.args[1], 8888)
        self.wrapper.assert_called_once()
        self.factory.return_value.close.assert_called_once()

    def test_missing_cli_warns_on_broad_bind(self):
        self.discovery.return_value = None
        self.factory.return_value.bind_ip = "0.0.0.0"
        self.assertEqual(lowtalk.main(), 0)
        self.assertEqual(self.factory.call_args.args[3], "0.0.0.0")
        self.assertTrue(any("ALL IPv4 interfaces" in call.args[1]
                            for call in self.ui.event.call_args_list))

    def test_unbindable_tailscale_address_falls_back(self):
        fallback = Mock(bind_ip="0.0.0.0")
        self.factory.side_effect = [OSError(errno.EADDRNOTAVAIL, "unavailable"), fallback]
        self.assertEqual(lowtalk.main(), 0)
        self.assertEqual(self.factory.call_count, 2)
        self.assertEqual(self.factory.call_args.args[3], "0.0.0.0")
        self.assertTrue(any("not bindable" in call.args[1]
                            for call in self.ui.event.call_args_list))
        fallback.close.assert_called_once()

    def test_shutdown_discard_warning_is_printed_after_curses_returns(self):
        self.factory.return_value.bind_ip = "100.64.0.1"
        self.wrapper.return_value = ["friend"]
        self.assertEqual(lowtalk.main(), 0)
        self.assertIn("unsent messages discarded on exit for: friend", self.stderr.getvalue())
        self.factory.return_value.close.assert_called_once()

    def test_occupied_port_never_falls_back(self):
        self.factory.side_effect = OSError(errno.EADDRINUSE, "occupied")
        self.assertEqual(lowtalk.main(), 1)
        self.factory.assert_called_once()
        self.wrapper.assert_not_called()
        self.assertIn("occupied", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
