"""Version-one wire format: newline-delimited UTF-8 JSON objects.

Frames are at most MAX_FRAME bytes, excluding the newline. Each direction starts
with {"type":"hello","version":1,"nick":"guest","port":7777}; Network checks
handshake order and the advertised port against its peer configuration. Later
frames are {"type":"message","text":"hello"}, {"type":"ping"}, or {"type":"pong"}.
Extra fields are ignored. Malformed framing or fields close the connection.
"""

from collections.abc import Iterator
import json
import re

MAX_FRAME = 16_384
MAX_MESSAGE = 4_000
MAX_NICK = 32


class ProtocolError(ValueError):
    pass


# Freeze the wire policy rather than using the interpreter's Unicode database:
# a new emoji is "unassigned" on older Python, not a reason to disconnect it.
# These are Unicode 16.0's Cc, Cf, and Cs ranges. Private-use and unassigned code
# points are allowed. Changes to this table are protocol decisions, not automatic
# Unicode upgrades; display widths may still differ between terminals.
_UNSAFE_TEXT = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u00ad\u0600-\u0605\u061c\u06dd\u070f"
    r"\u0890-\u0891\u08e2\u180e\u200b-\u200f\u202a-\u202e"
    r"\u2060-\u2064\u2066-\u206f\ud800-\udfff\ufeff\ufff9-\ufffb"
    r"\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3"
    r"\U0001d173-\U0001d17a\U000e0001\U000e0020-\U000e007f]"
)


def clean_text(text: str) -> str:
    """Strip terminal controls, surrogate code points, and formatting controls."""
    return _UNSAFE_TEXT.sub("", text)


def valid_text(value: object, limit: int) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and len(value) <= limit and clean_text(value) == value)


def encode(message: dict) -> bytes:
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ProtocolError("message exceeds wire-size limit")
    return data + b"\n"


def decode(data: bytes) -> dict:
    try:
        message = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("invalid JSON frame") from exc
    if not isinstance(message, dict):
        raise ProtocolError("expected an object")
    kind = message.get("type")
    if kind == "hello":
        port = message.get("port")
        version = message.get("version")
        if (type(version) is not int or version != 1
                or not valid_text(message.get("nick"), MAX_NICK)
                or type(port) is not int or not 1 <= port <= 65535):
            raise ProtocolError("invalid hello")
    elif kind == "message":
        if not valid_text(message.get("text"), MAX_MESSAGE):
            raise ProtocolError("invalid message text")
    elif kind not in ("ping", "pong"):
        raise ProtocolError("unknown message type")
    return message


class FrameReader:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> Iterator[dict]:
        # Consume this iterator before feeding again (or abandon the connection).
        # Yield valid frames before parsing the next: a bad suffix must not erase
        # earlier messages just because TCP coalesced them into one recv().
        self.buffer.extend(data)
        while True:
            end = self.buffer.find(b"\n")
            if end < 0:
                if len(self.buffer) > MAX_FRAME:
                    raise ProtocolError("frame too large")
                break
            if end > MAX_FRAME:
                raise ProtocolError("frame too large")
            frame = bytes(self.buffer[:end])
            del self.buffer[:end + 1]
            yield decode(frame)
