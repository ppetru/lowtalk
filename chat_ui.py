"""Compact curses interface and a deliberately small readline-style editor."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
import time
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chat_network import Network

from chat_protocol import MAX_MESSAGE, clean_text


def cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def clip(text: str, width: int) -> str:
    result = []
    used = 0
    for char in text:
        size = cell_width(char)
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result)


def wrap(text: str, width: int) -> list[str]:
    """Wrap by terminal cells, including wide characters, without losing spaces."""
    width = max(2, width)
    lines = []
    line = ""
    used = 0
    for char in text:
        size = cell_width(char)
        if used + size > width:
            lines.append(line)
            line, used = "", 0
        line += char
        used += size
    lines.append(line)
    return lines


@dataclass
class Editor:
    text: str = ""
    cursor: int = 0
    killed: str = ""

    def insert(self, text: str) -> None:
        text = clean_text(text)
        available = MAX_MESSAGE - len(self.text)
        text = text[:available]
        self.text = self.text[:self.cursor] + text + self.text[self.cursor:]
        self.cursor += len(text)

    def kill(self, start: int, end: int) -> None:
        self.killed = self.text[start:end]
        self.text = self.text[:start] + self.text[end:]
        self.cursor = start

    def key(self, key: str) -> None:
        if key in ("LEFT", "\x02"):
            self.cursor = max(0, self.cursor - 1)
        elif key in ("RIGHT", "\x06"):
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key in ("HOME", "\x01"):
            self.cursor = 0
        elif key in ("END", "\x05"):
            self.cursor = len(self.text)
        elif key in ("BACKSPACE", "\x7f", "\x08"):
            if self.cursor:
                self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
                self.cursor -= 1
        elif key in ("DELETE", "\x04"):
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        elif key == "\x0b":
            self.kill(self.cursor, len(self.text))
        elif key == "\x15":
            self.kill(0, self.cursor)
        elif key == "\x17":
            start = self.cursor
            while start and self.text[start - 1].isspace():
                start -= 1
            while start and not self.text[start - 1].isspace():
                start -= 1
            self.kill(start, self.cursor)
        elif key == "\x19":
            self.insert(self.killed)
        elif len(key) == 1 and key.isprintable():
            self.insert(key)

    def take(self) -> str:
        text = self.text
        self.text = ""
        self.cursor = 0
        return text


class ChatUI:
    def __init__(self, nick: str) -> None:
        self.nick = nick
        # Assigned by the entry point after the listener is successfully bound.
        self.network: Network
        self.editor = Editor()
        self.messages: deque[tuple[int, str]] = deque(maxlen=1000)
        self.sequence = 0
        # Anchor by message and wrapped-line index, not distance from the end.
        # Incoming messages therefore don't move the currently viewed content.
        self.anchor: tuple[int, int] | None = None
        self.unread = 0
        self.page_height = 1
        self.rendered: list[tuple[tuple[int, int], str]] = []
        self.view_start = 0
        self.layout_width = 0
        self.layout_sequence = -1

    def event(self, sender: str, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M")
        self.messages.append((self.sequence, f"{stamp} [{clean_text(sender)}] {clean_text(text)}"))
        self.sequence += 1
        if self.anchor is not None:
            self.unread += 1

    def _send(self) -> bool:
        text = self.editor.take()
        if text == "/quit":
            return False
        if text == "/who":
            for peer in self.network.peers:
                address = (
                    peer.connection.address if peer.connection
                    else ", ".join(peer.config.addresses)
                )
                detail = f"; last error: {peer.last_error}" if peer.last_error else ""
                self.event("*", f"{peer.nick or peer.config.label} @ {address}: "
                           f"{peer.status} (port {peer.config.port}){detail}")
        elif text.startswith("/") and not text.startswith("//"):
            self.event("!", "Commands: /who, /quit. Use // to send a leading slash.")
        elif text.strip():
            if text.startswith("//"):
                text = text[1:]
            queued, unavailable = self.network.broadcast(text)
            self.event(f"{self.nick} (you)", text)
            if unavailable:
                self.event("!", "Not queued for: " + ", ".join(unavailable))
            if not queued:
                self.event("!", "No connected recipients; message was not sent.")
        return True

    def _scroll(self, direction: int) -> None:
        if not self.rendered:
            return
        bottom = max(0, len(self.rendered) - self.page_height)
        target = min(bottom, max(0, self.view_start + direction * self.page_height))
        self.anchor = self.rendered[target][0] if target < bottom else None
        if self.anchor is None:
            self.unread = 0

    def run(self, screen) -> None:
        import curses

        curses.raw()  # Handle Ctrl-C ourselves; Ctrl-S/Q must not freeze the UI.
        screen.keypad(True)
        screen.nodelay(True)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        keys = {
            curses.KEY_LEFT: "LEFT", curses.KEY_RIGHT: "RIGHT",
            curses.KEY_HOME: "HOME", curses.KEY_END: "END",
            curses.KEY_BACKSPACE: "BACKSPACE", curses.KEY_DC: "DELETE",
        }
        running = True
        while running:
            self.network.tick()
            # Bound key work too, so pasted input cannot starve networking.
            for _ in range(64):
                try:
                    key = screen.get_wch()
                except curses.error:
                    break
                if key == "\x03":
                    running = False
                    break
                if key in ("\n", "\r", curses.KEY_ENTER):
                    running = self._send()
                    if not running:
                        break
                elif key == curses.KEY_PPAGE:
                    self._scroll(-1)
                elif key == curses.KEY_NPAGE:
                    self._scroll(1)
                elif key == "\x0c":
                    screen.clearok(True)
                elif key in keys:
                    self.editor.key(keys[key])
                elif isinstance(key, str):
                    self.editor.key(key)
            self.draw(screen)
            time.sleep(0.03)

    def draw(self, screen) -> None:
        import curses

        height, width = screen.getmaxyx()
        screen.erase()

        def put(row: int, text: str, attr: int = 0) -> None:
            if 0 <= row < height and width > 1:
                try:
                    screen.addstr(row, 0, clip(text, width - 1), attr)
                except curses.error:
                    pass  # A resize may happen between getmaxyx and addstr.

        if height < 5 or width < 20:
            put(0, "Resize terminal (20x5 minimum)")
            screen.refresh()
            return

        friends = " | ".join(
            f"{peer.nick or peer.config.label}: {peer.status}"
            for peer in self.network.peers
        )
        # At most two rows: the common one-friend case costs just one row.
        status_lines = wrap(friends, width - 1)
        status_height = min(2, len(status_lines), height - 4)
        for row in range(status_height):
            line = status_lines[row]
            if row == status_height - 1 and len(status_lines) > status_height:
                line = clip(line, width - 5) + " ..."
            put(row, line, curses.A_REVERSE)
        self.page_height = height - status_height - 2
        if self.layout_width != width:
            self.rendered = []
            self.layout_sequence = -1
            self.layout_width = width
        if self.messages and self.layout_sequence != self.messages[-1][0]:
            oldest = self.messages[0][0]
            self.rendered = [entry for entry in self.rendered if entry[0][0] >= oldest]
            for seq, message in self.messages:
                if seq > self.layout_sequence:
                    self.rendered.extend(
                        ((seq, index), line)
                        for index, line in enumerate(wrap(message, width - 1))
                    )
            self.layout_sequence = self.messages[-1][0]
        bottom = max(0, len(self.rendered) - self.page_height)
        self.view_start = bottom
        if self.anchor is not None:
            self.view_start = next(
                (i for i, (position, _) in enumerate(self.rendered) if position >= self.anchor),
                bottom,
            )
            self.view_start = min(bottom, self.view_start)
        for row, (_, line) in enumerate(
                self.rendered[self.view_start:self.view_start + self.page_height]):
            put(status_height + row, line)
        hint = "PgUp/PgDn history | /who /quit | Ctrl-C quit"
        if self.anchor is not None:
            hint = f"History: {self.unread} new | PgDn to return | " + hint
        put(height - 2, hint, curses.A_DIM)
        prompt = clip(f"[{self.nick}] ", max(4, width // 3))
        room = width - 1 - cell_width(prompt)
        start = self.editor.cursor
        used = 0
        while start > 0:
            size = cell_width(self.editor.text[start - 1])
            if used + size >= room:
                break
            used += size
            start -= 1
        put(height - 1, prompt + clip(self.editor.text[start:], room))
        try:
            screen.move(height - 1, cell_width(prompt) + used)
        except curses.error:
            pass
        screen.refresh()
