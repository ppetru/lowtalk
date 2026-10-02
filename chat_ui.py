"""Compact curses interface and a deliberately small readline-style editor."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
import time
import unicodedata
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import curses
    from chat_network import Network

from chat_protocol import MAX_MESSAGE, clean_text


def cell_width(text: str) -> int:
    # Code-point editing and approximate cell widths, not grapheme segmentation.
    # Combining marks/East Asian widths cover ordinary text; complex emoji may
    # differ from the terminal's rendering without changing the wire policy.
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def clip(text: str, width: int) -> str:
    result: list[str] = []
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
    lines: list[str] = []
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
    truncated: bool = False

    def insert(self, text: str) -> None:
        text = clean_text(text)
        available = MAX_MESSAGE - len(self.text)
        # Sticky for this draft: editing afterwards cannot recover discarded text.
        self.truncated |= len(text) > available
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
        elif len(key) == 1:
            # Use the wire policy, not isprintable(), which varies with Python's
            # Unicode version and rejects newer emoji on older interpreters.
            self.insert(key)

    def take(self) -> str:
        text = self.text
        self.text = ""
        self.cursor = 0
        self.truncated = False
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
        self.anchor: Optional[tuple[int, int]] = None
        self.unread = 0
        self.page_height = 1
        self.rendered: list[tuple[tuple[int, int], str]] = []
        self.view_start: Optional[tuple[int, int]] = None
        self.layout_width = 0
        # Wrap only the viewport and its immediate navigation targets. Retaining
        # every wrapped row can cost hundreds of thousands of objects; rebuilding
        # them on resize blocks this same thread's input and network processing.
        self.wrapped: OrderedDict[int, list[str]] = OrderedDict()

    def event(self, sender: str, text: str) -> None:
        if len(self.messages) == self.messages.maxlen:
            self.wrapped.pop(self.messages[0][0], None)
        # Local receipt/send-attempt time, never an untrusted sender timestamp.
        stamp = datetime.now().strftime("%H:%M")
        text = clean_text(text)
        line = (f"{stamp} --- {text} ---" if sender == "---"
                else f"{stamp} [{clean_text(sender)}] {text}")
        self.messages.append((self.sequence, line))
        self.sequence += 1
        if self.anchor is not None:
            self.unread += 1

    def _send(self) -> bool:
        truncated = self.editor.truncated
        text = self.editor.text
        if text == "/quit":
            self.editor.take()
            return False
        if text == "/who":
            for peer in self.network.peers:
                address = (
                    peer.connection.address if peer.connection
                    else ", ".join(peer.config.addresses)
                )
                detail = f"; last error: {peer.last_error}" if peer.last_error else ""
                self.event("*", f"{peer.nick or peer.config.label} @ {address}: "
                           f"{peer.display_status} (port {peer.config.port}){detail}")
        elif text.startswith("/") and not text.startswith("//"):
            self.event("!", "Commands: /who, /quit. Use // to send a leading slash.")
        elif text.strip():
            payload = text[1:] if text.startswith("//") else text
            queued, unavailable = self.network.broadcast(payload)
            if not queued:
                self.event("!", "Not sent: no recipients queued. "
                           "Draft kept; press Enter to retry.")
                if unavailable:
                    self.event("!", "Not queued for: " + ", ".join(unavailable))
                return True
            self.event(f"{self.nick} (you)", payload)
            if unavailable:
                self.event("!", "Not queued for: " + ", ".join(unavailable))
        self.editor.take()
        if truncated:
            # A paste can include Enter before redraw. Put persistent feedback
            # AFTER the long local echo so it stays visible in the live viewport.
            self.event("!", f"Input truncated at {MAX_MESSAGE:,} characters; "
                       "excess input was discarded.")
        return True

    def _wrapped(self, seq: int) -> list[str]:
        if seq not in self.wrapped:
            message = self.messages[seq - self.messages[0][0]][1]
            self.wrapped[seq] = wrap(message, self.layout_width - 1)
            if len(self.wrapped) > 64:
                self.wrapped.popitem(last=False)
        self.wrapped.move_to_end(seq)
        return self.wrapped[seq]

    def _position(self, position: tuple[int, int]) -> tuple[int, int]:
        seq, line = position
        if seq < self.messages[0][0]:
            return self.messages[0][0], 0  # The anchored message was evicted.
        seq = min(seq, self.messages[-1][0])
        return seq, min(line, len(self._wrapped(seq)) - 1)

    def _move(self, position: tuple[int, int], rows: int) -> tuple[int, int]:
        seq, line = self._position(position)
        while rows < 0:
            if -rows <= line:
                return seq, line + rows
            if seq == self.messages[0][0]:
                return seq, 0
            rows += line + 1
            seq -= 1
            line = len(self._wrapped(seq)) - 1
        while rows > 0:
            remaining = len(self._wrapped(seq)) - 1 - line
            if rows <= remaining:
                return seq, line + rows
            if seq == self.messages[-1][0]:
                return seq, line + remaining
            rows -= remaining + 1
            seq += 1
            line = 0
        return seq, line

    def _bottom(self) -> tuple[int, int]:
        seq = self.messages[-1][0]
        return self._move((seq, len(self._wrapped(seq)) - 1), 1 - self.page_height)

    def _scroll(self, direction: int) -> None:
        if not self.messages or not self.layout_width:
            return
        bottom = self._bottom()
        start = min(self._position(self.anchor), bottom) if self.anchor else bottom
        target = min(bottom, self._move(start, direction * self.page_height))
        # Update the navigation state now, not on redraw: multiple keys may be
        # consumed in one input batch, and each must advance from the last one.
        self.view_start = target
        self.anchor = target if target < bottom else None
        if self.anchor is None:
            self.unread = 0

    def run(self, screen: curses.window) -> list[str]:
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
                elif isinstance(key, int) and key in keys:
                    self.editor.key(keys[key])
                elif isinstance(key, str):
                    self.editor.key(key)
            self.draw(screen)
            time.sleep(0.03)
        return self.network.flush()

    def draw(self, screen: curses.window) -> None:
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
            f"{peer.nick or peer.config.label}: {peer.display_status}"
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
            self.wrapped.clear()
            self.layout_width = width
        self.rendered = []
        if self.messages:
            bottom = self._bottom()
            self.view_start = (
                min(self._position(self.anchor), bottom) if self.anchor else bottom
            )
            if self.anchor is not None:
                self.anchor = self.view_start
            seq, index = self.view_start
            while seq <= self.messages[-1][0] and len(self.rendered) < self.page_height:
                lines = self._wrapped(seq)
                count = min(len(lines) - index, self.page_height - len(self.rendered))
                self.rendered.extend(
                    ((seq, i), lines[i]) for i in range(index, index + count)
                )
                seq, index = seq + 1, 0
        for row, (_, line) in enumerate(self.rendered):
            put(status_height + row, line)
        hint = "PgUp/PgDn history | /who /quit | Ctrl-C quit"
        if self.anchor is not None:
            hint = f"History: {self.unread} new | PgDn to return | " + hint
        if self.editor.truncated:
            hint = f"Input truncated ({MAX_MESSAGE:,} max) | " + hint
        put(height - 2, hint, curses.A_BOLD if self.editor.truncated else curses.A_DIM)
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
