"""Version-two wire format: newline-delimited UTF-8 JSON objects.

Frames are at most MAX_FRAME bytes, excluding the newline. Each direction starts
with a hello carrying version, nickname, listening port, and a random process
instance. The smaller instance coordinates select -> accept -> ready on one
candidate socket. Only then may either side send message, ping, or pong frames.
Extra fields are ignored. Malformed framing or fields close the connection.
"""

from collections.abc import Iterator
import json
import re
from typing import Literal, TypedDict, Union, cast

MAX_FRAME = 16_384
MAX_MESSAGE = 4_000
MAX_NICK = 32



class HelloFrame(TypedDict):
    type: Literal["hello"]
    version: Literal[2]
    nick: str
    port: int
    instance: str


class SelectFrame(TypedDict):
    type: Literal["select"]


class AcceptFrame(TypedDict):
    type: Literal["accept"]


class ReadyFrame(TypedDict):
    type: Literal["ready"]


class MessageFrame(TypedDict):
    type: Literal["message"]
    text: str


class PingFrame(TypedDict):
    type: Literal["ping"]


class PongFrame(TypedDict):
    type: Literal["pong"]


Frame = Union[
    HelloFrame, SelectFrame, AcceptFrame, ReadyFrame, MessageFrame, PingFrame, PongFrame
]

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


def encode(message: Frame) -> bytes:
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ProtocolError("message exceeds wire-size limit")
    return data + b"\n"


def decode(data: bytes) -> Frame:
    try:
        raw: object = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("invalid JSON frame") from exc
    if not isinstance(raw, dict):
        raise ProtocolError("expected an object")
    # JSON object keys are strings. Values remain untrusted objects until the
    # checks below; construct fresh typed frames rather than trusting raw fields.
    message = cast(dict[str, object], raw)
    kind = message.get("type")
    if kind == "hello":
        version = message.get("version")
        if type(version) is not int:
            raise ProtocolError("invalid hello version")
        if version != 2:
            raise ProtocolError(f"unsupported protocol version {version}; expected 2")
        port = message.get("port")
        nick = message.get("nick")
        instance = message.get("instance")
        if (type(port) is not int or not 1 <= port <= 65535
                or not isinstance(nick, str) or not valid_text(nick, MAX_NICK)
                or not isinstance(instance, str)
                or re.fullmatch(r"[0-9a-f]{32}", instance) is None):
            raise ProtocolError("invalid hello")
        return {"type": "hello", "version": 2, "nick": nick,
                "port": port, "instance": instance}
    if kind == "message":
        text = message.get("text")
        if not isinstance(text, str) or not valid_text(text, MAX_MESSAGE):
            raise ProtocolError("invalid message text")
        return {"type": "message", "text": text}
    if kind == "select":
        return {"type": "select"}
    if kind == "accept":
        return {"type": "accept"}
    if kind == "ready":
        return {"type": "ready"}
    if kind == "ping":
        return {"type": "ping"}
    if kind == "pong":
        return {"type": "pong"}
    raise ProtocolError("unknown message type")


class FrameReader:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> Iterator[Frame]:
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
