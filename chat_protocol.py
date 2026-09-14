"""Version-one wire format: bounded, newline-delimited UTF-8 JSON objects."""

import json
import unicodedata

MAX_FRAME = 16_384
MAX_MESSAGE = 4_000
MAX_NICK = 32


class ProtocolError(ValueError):
    pass


def clean_text(text: str) -> str:
    """Strip terminal controls, surrogate code points, and formatting controls."""
    return "".join(char for char in text if not unicodedata.category(char).startswith("C"))


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

    def feed(self, data: bytes) -> list[dict]:
        self.buffer.extend(data)
        messages = []
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
            messages.append(decode(frame))
        return messages
