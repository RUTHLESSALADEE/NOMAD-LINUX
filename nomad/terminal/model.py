"""The terminal's contents: a VT100/xterm emulator (pyte) with scrollback, the alternate screen full-screen programs
use, selection and search, plus what each key should send. No Qt here, so it can be tested on its own.
"""
import codecs
import copy
import re
from dataclasses import dataclass

import pyte

DECCKM = 1 << 5  # Application cursor keys (pyte stores private modes shifted left by 5)
BRACKETED_PASTE = 2004 << 5
ALTERNATE_SCREEN_MODES = {47, 1047, 1049}  # vim, less, top and friends draw on a separate screen
ESCAPE_SEQUENCE = (r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[PX^_][^\x1b]*\x1b\\|[()*+][0-9A-Za-z]|"
                   r"[@-Z\\-_])")
ESCAPE_PATTERN = re.compile(ESCAPE_SEQUENCE)
ANSI_PATTERN = re.compile(ESCAPE_SEQUENCE + r"|[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")  # Plus control characters


class TerminalScreen(pyte.HistoryScreen):
    """pyte's screen with scrollback, plus the alternate screen buffer (which pyte doesn't have)."""

    def __init__(self, columns, lines, history, respond=lambda text: None):
        super().__init__(columns, lines, history=history, ratio=0.5)
        self.respond = respond
        self.saved_main = None  # (lines, cursor) of the normal screen while the alternate one is showing

    @property
    def alternate(self):
        return self.saved_main is not None

    def write_process_input(self, data):
        self.respond(data)  # Answers to the device's questions, such as "where is the cursor?"

    def set_mode(self, *modes, **kwargs):
        if kwargs.get("private") and ALTERNATE_SCREEN_MODES & set(modes):
            if self.saved_main is None:
                lines = {y: dict(line) for y, line in self.buffer.items()}
                self.saved_main = (lines, copy.copy(self.cursor))
                self.erase_in_display(2)
                self.cursor_position()
            modes = tuple(mode for mode in modes if mode not in ALTERNATE_SCREEN_MODES)
        if modes:
            super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        if kwargs.get("private") and ALTERNATE_SCREEN_MODES & set(modes):
            if self.saved_main is not None:
                lines, cursor = self.saved_main
                self.saved_main = None
                self.buffer.clear()
                for y, chars in lines.items():
                    self.buffer[y].update(chars)
                self.cursor = cursor
                self.ensure_vbounds()
                self.ensure_hbounds()
                self.dirty.update(range(self.lines))
            modes = tuple(mode for mode in modes if mode not in ALTERNATE_SCREEN_MODES)
        if modes:
            super().reset_mode(*modes, **kwargs)

    def index(self):
        if self.alternate:
            pyte.Screen.index(self)  # Full-screen programs' scrolling doesn't belong in the scrollback
        else:
            top, bottom = self.margins or pyte.screens.Margins(0, self.lines - 1)
            if self.cursor.y == bottom:  # pyte's own test for adding a line to the scrollback
                self.scrolled_out += 1
            super().index()

    scrolled_out = 0  # Lines ever added to the scrollback: once it's full, the oldest go as new ones arrive

    def resize(self, lines=None, columns=None):
        """Change the size as xterm and Windows Terminal do, not as pyte does (pyte throws away lines from the top
        and leaves the cursor where the prompt was): shrinking moves only as many top lines into the scrollback as
        keep the cursor on screen, and growing brings them back. A screen with room to spare doesn't move, and
        growing back to the old size restores exactly what was shown."""
        lines = lines or self.lines
        if lines != self.lines:
            if lines < self.lines:
                self._move_lines_up(max(0, self.cursor.y - (lines - 1)), lines)
            elif not self.alternate:
                self._move_lines_down(min(lines - self.lines, len(self.history.top)))
            self.lines = lines  # So pyte only deals with the columns
            self.set_margins()
            self.dirty.update(range(lines))
        super().resize(lines, columns)

    def _move_lines_up(self, count, lines):
        """Shift the screen up `count` lines (into the scrollback, unless on the alternate screen), keeping
        `lines` of it."""
        if count:
            if not self.alternate:
                for y in range(count):
                    self.history.top.append(self.buffer[y])
                self.scrolled_out += count
            self.cursor.y -= count
        kept = {y - count: line for y, line in self.buffer.items() if count <= y < count + lines}
        self.buffer.clear()
        self.buffer.update(kept)

    def _move_lines_down(self, count):
        """Bring the last `count` scrollback lines back onto the top of the screen."""
        if not count:
            return
        moved = {y + count: line for y, line in self.buffer.items()}
        self.buffer.clear()
        self.buffer.update(moved)
        for y in range(count - 1, -1, -1):
            self.buffer[y] = self.history.top.pop()
        self.scrolled_out -= count
        self.cursor.y += count

    def prev_page(self):
        pass  # The view scrolls through history itself; pyte's own paging would rewrite the screen

    def next_page(self):
        pass


@dataclass(frozen=True)
class Position:
    line: int  # Index into history + screen lines
    column: int

    def __lt__(self, other):
        return (self.line, self.column) < (other.line, other.column)

    def __le__(self, other):
        return (self.line, self.column) <= (other.line, other.column)


class TerminalModel:
    def __init__(self, columns=80, rows=24, scrollback=10000, encoding="utf-8", respond=lambda text: None):
        self.screen = TerminalScreen(columns, rows, max(scrollback, 0) or 1, respond)
        self.stream = pyte.Stream(self.screen)
        self.encoding = encoding
        self.decoder = codecs.getincrementaldecoder(encoding)(errors="replace")

    # ----------------------------------------------------------------- Input from the device

    def feed(self, data):
        """Process bytes from the device. Returns the decoded text (for logging)."""
        text = self.decoder.decode(data)
        if text:
            self.stream.feed(text)
        return text

    def feed_text(self, text):
        self.stream.feed(text)

    def resize(self, columns, rows):
        if (columns, rows) != (self.screen.columns, self.screen.lines):
            self.screen.resize(rows, columns)

    def reset(self):
        self.screen.reset()
        self.screen.history.top.clear()
        self.screen.saved_main = None

    def clear_scrollback(self):
        self.screen.history.top.clear()

    # ----------------------------------------------------------------- Reading the contents

    @property
    def columns(self):
        return self.screen.columns

    @property
    def rows(self):
        return self.screen.lines

    @property
    def history_length(self):
        return len(self.screen.history.top)

    @property
    def scrolled_out(self):
        """How many lines have ever scrolled into the scrollback (it keeps counting once the scrollback is full)."""
        return self.screen.scrolled_out

    @property
    def line_count(self):
        return self.history_length + self.rows

    @property
    def application_cursor(self):
        return DECCKM in self.screen.mode

    @property
    def bracketed_paste(self):
        return BRACKETED_PASTE in self.screen.mode

    @property
    def cursor_visible(self):
        return not self.screen.cursor.hidden

    def line(self, index):
        """The characters of a line (history first, then the screen), as {column: pyte Char}."""
        history = self.screen.history.top
        if index < len(history):
            return history[index]
        return self.screen.buffer[index - len(history)]

    def line_text(self, index, start=0, end=None):
        line = self.line(index)
        end = self.columns if end is None else end
        return "".join(line[column].data for column in range(start, end)).rstrip()

    def cursor_position(self):
        return Position(self.history_length + self.screen.cursor.y, self.screen.cursor.x)

    def text_between(self, start, end):
        """The text from start to end (inclusive), one line per row, trailing spaces removed."""
        if end < start:
            start, end = end, start
        lines = []
        for index in range(start.line, end.line + 1):
            first = start.column if index == start.line else 0
            last = end.column + 1 if index == end.line else self.columns
            lines.append(self.line_text(index, first, last))
        return "\n".join(lines)

    def word_at(self, position):
        """The start and end of the word under a position, for double-click selection."""
        text = "".join(self.line(position.line)[column].data or " " for column in range(self.columns))
        column = min(position.column, len(text) - 1)
        if column < 0 or not text[column].strip():
            return position, position
        separators = " \t\"'`()[]{}<>,;|"
        start = column
        while start > 0 and text[start - 1] not in separators:
            start -= 1
        end = column
        while end < len(text) - 1 and text[end + 1] not in separators:
            end += 1
        return Position(position.line, start), Position(position.line, end)

    def line_at(self, position):
        """The start and end of the text on the line under a position, for triple-click selection."""
        text = self.line_text(position.line)
        if not text:
            return position, position
        return Position(position.line, 0), Position(position.line, len(text) - 1)

    def find(self, query, before=None):
        """The last match of query (ignoring case) that starts before `before` (or anywhere), or None."""
        query = query.lower()
        if not query:
            return None
        last_line = self.line_count - 1 if before is None else min(before.line, self.line_count - 1)
        for index in range(last_line, -1, -1):
            text = "".join(self.line(index)[column].data or " " for column in range(self.columns)).lower()
            limit = before.column if before is not None and index == before.line else len(text)
            found = text.rfind(query, 0, limit + len(query) - 1)  # Only matches that start before limit
            if 0 <= found < limit:
                return Position(index, found)
        return None


# ----------------------------------------------------------------- Keyboard

KEYS = {
    "Enter": "\r", "Tab": "\t", "Escape": "\x1b", "Insert": "\x1b[2~", "Delete": "\x1b[3~",
    "PageUp": "\x1b[5~", "PageDown": "\x1b[6~", "Home": "\x1b[H", "End": "\x1b[F",
    "F1": "\x1bOP", "F2": "\x1bOQ", "F3": "\x1bOR", "F4": "\x1bOS", "F5": "\x1b[15~", "F6": "\x1b[17~",
    "F7": "\x1b[18~", "F8": "\x1b[19~", "F9": "\x1b[20~", "F10": "\x1b[21~", "F11": "\x1b[23~", "F12": "\x1b[24~",
    "Backtab": "\x1b[Z",
}
ARROWS = {"Up": "A", "Down": "B", "Right": "C", "Left": "D"}


def encode_key(key, text="", ctrl=False, alt=False, shift=False, application_cursor=False,
               backspace_delete=True, enter="\r"):
    """What a key press should send, as text (encode it with the session's encoding). "" for nothing.

    key is a name like "Up", "F5", "Enter", "Backspace", or "" for an ordinary character given in text.
    """
    if key in ARROWS:
        letter = ARROWS[key]
        if ctrl or alt or shift:  # xterm's modified cursor keys, used by shells to move by word
            modifier = 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)
            return f"\x1b[1;{modifier}{letter}"
        return ("\x1bO" if application_cursor else "\x1b[") + letter
    if key in ("Home", "End") and application_cursor and not (ctrl or alt or shift):
        return "\x1bO" + ("H" if key == "Home" else "F")
    if key == "Backspace":
        code = "\x7f" if backspace_delete else "\x08"
        if ctrl:
            code = "\x08" if backspace_delete else "\x7f"
        return ("\x1b" if alt else "") + code
    if key == "Enter":
        return ("\x1b" if alt else "") + enter
    if key in KEYS:
        return KEYS[key]
    if not text:
        return ""
    if ctrl and not alt and len(text) == 1:
        character = text
        if "a" <= character.lower() <= "z":
            return chr(ord(character.lower()) - ord("a") + 1)
        controls = {" ": "\x00", "2": "\x00", "@": "\x00", "[": "\x1b", "\\": "\x1c", "]": "\x1d", "^": "\x1e",
                    "6": "\x1e", "_": "\x1f", "-": "\x1f", "/": "\x1f", "?": "\x7f"}
        if character in controls:
            return controls[character]
    if alt and not ctrl:
        return "\x1b" + text  # Meta: ESC before the key, as xterm and PuTTY do
    return text


def encode_paste(text, bracketed=False):
    """Pasted text: line breaks become Enter (CR), and programs that ask are told it's a paste."""
    text = text.replace("\r\n", "\r").replace("\n", "\r")
    if bracketed:
        return "\x1b[200~" + text.replace("\x1b[201~", "") + "\x1b[201~"
    return text


# ----------------------------------------------------------------- Logging

class LogCleaner:
    """Turns terminal output into plain text for a log file: escape sequences removed, line ends tidied."""

    def __init__(self):
        self.pending = ""

    def clean(self, text):
        text = self.pending + text
        # Keep an escape sequence that's cut off at the end for next time
        cut = text.rfind("\x1b")
        if cut >= 0 and len(text) - cut < 32 and not ESCAPE_PATTERN.match(text, cut):
            text, self.pending = text[:cut], text[cut:]
        else:
            self.pending = ""
        text = ANSI_PATTERN.sub(lambda match: "" if match.group(0) not in "\r\n\t" else match.group(0), text)
        return text.replace("\r\n", "\n").replace("\r", "")
