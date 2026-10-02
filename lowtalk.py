#!/usr/bin/env python3
"""Lowtalk: dependency-free, ephemeral terminal chat over Tailscale."""

from __future__ import annotations

import argparse
import errno
from pathlib import Path
import signal
import sys
from types import FrameType

from chat_config import DEFAULT_PORT, load_peers, parse_port, tailscale_ip
from chat_network import Network
from chat_protocol import MAX_NICK, valid_text
from chat_ui import ChatUI

# Runtime and tests use only the standard library, from the repository root:
#   python3 -m unittest discover -s tests -v
# Development-only static checks: pyright; ruff check .
# Tests use loopback sockets and a PTY, never the local friends file or Tailscale.


class Arguments(argparse.Namespace):
    nick: str
    port: int


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("nick", help="self-chosen nickname (up to 32 characters)")
    parser.add_argument("port", nargs="?", default=DEFAULT_PORT, type=parse_port,
                        help="local listening port (default: 7777)")
    args = parser.parse_args(namespace=Arguments())
    if not valid_text(args.nick, MAX_NICK):
        parser.error("nickname must be 1–32 characters, nonblank, without control characters")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("an interactive terminal is required")
    try:
        import curses
    except ImportError:
        print(
            "Error: this Python build lacks curses; use a Linux/macOS Python with curses support.",
            file=sys.stderr,
        )
        return 1

    network = None
    try:
        # DNS and optional CLI discovery may block; finish both before curses.
        peers = load_peers(Path.cwd() / "friends_tailscale_ips.txt")
        local_ip = tailscale_ip()
        if local_ip and any(
            local_ip in peer.addresses and peer.port == args.port for peer in peers
        ):
            raise ValueError(
                "peer file includes this app's own address and port; list friends only"
            )
        ui = ChatUI(args.nick)
        warning = None
        try:
            network = Network(args.nick, args.port, peers, local_ip or "0.0.0.0", ui.event)
        except OSError as exc:
            # Some macOS Tailscale installations do not expose a bindable IP.
            # Never hide EADDRINUSE by retrying on a broader interface.
            if not local_ip or exc.errno != errno.EADDRNOTAVAIL:
                raise
            network = Network(args.nick, args.port, peers, "0.0.0.0", ui.event)
            warning = f"Tailscale IP {local_ip} is not bindable."
        if network.bind_ip == "0.0.0.0":
            warning = (warning or "Could not discover local Tailscale IPv4 address.") + (
                " Listening on ALL IPv4 interfaces; peer allowlist still enforced."
            )
        ui.network = network
        ui.event("*", f"Listening on {network.bind_ip}:{args.port}. No logs or offline queue.")
        if warning:
            ui.event("!", warning)
        ui.event("*", "TCP is not a display receipt. /who lists peers; /quit exits.")

        def terminate(signum: int, frame: FrameType | None) -> None:
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGTERM, terminate)
        try:
            curses.wrapper(ui.run)
        finally:
            signal.signal(signal.SIGTERM, previous)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, curses.error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if network is not None:
            network.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
