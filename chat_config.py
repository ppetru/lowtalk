"""Peer-file parsing and best-effort local Tailscale address discovery."""

from dataclasses import dataclass
import ipaddress
from pathlib import Path
import socket
import subprocess

DEFAULT_PORT = 7777


@dataclass(frozen=True)
class PeerConfig:
    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def label(self) -> str:
        return self.host if self.port == DEFAULT_PORT else f"{self.host}:{self.port}"


def parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise ValueError(f"invalid port: {value!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"port must be between 1 and 65535: {value!r}")
    return port


def load_peers(path: Path) -> list[PeerConfig]:
    """Resolve once at startup. Ambiguous address membership is an error."""
    peers = []
    owners: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.partition("#")[0].split()
        if not fields:
            continue
        try:
            if len(fields) not in (1, 2):
                raise ValueError("expected HOST [PORT]")
            host = fields[0]
            port = parse_port(fields[1]) if len(fields) == 2 else DEFAULT_PORT
            results = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
            addresses = tuple(sorted({result[4][0] for result in results}))
            for address in addresses:
                if address in owners:
                    raise ValueError(f"{host} overlaps with {owners[address]} at {address}")
                owners[address] = host
            peers.append(PeerConfig(host, port, addresses))
        except (ValueError, OSError) as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
    if not peers:
        raise ValueError(f"{path}: no peers configured")
    return peers


def tailscale_ip() -> str | None:
    """The CLI is optional; never guess using an unrelated network interface."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True,
            timeout=3, check=True,
        )
        for line in result.stdout.splitlines():
            address = ipaddress.IPv4Address(line.strip())
            if address in ipaddress.IPv4Network("100.64.0.0/10"):
                return str(address)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None
