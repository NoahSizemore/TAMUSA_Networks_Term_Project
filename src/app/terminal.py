"""Terminal plumbing for the quiz: raw keyboard input and a diffing screen.

THIS MODULE CONTAINS NO NETWORKING.  It imports only os, select, shutil, sys
and the platform's terminal module (termios/tty on POSIX, msvcrt on Windows).
Every byte the application puts on or takes off the network goes through
transport.py and nothing else, which is the rule the project description sets
for the application half.

Two things live here:

  Keyboard  non-blocking single-keystroke input.  On POSIX it saves
            termios.tcgetattr(fd), calls tty.setraw, and restores the saved
            attributes in a finally block, so a crash cannot leave a shell
            that echoes nothing.  Used as a context manager.

  Screen    a line buffer that redraws only the lines that changed.  The quiz
            reprints its scoreboard many times a second; addressing the two or
            three lines that actually moved costs about 18 bytes each instead
            of a few kilobytes of escape sequences, which matters over a
            wide-area SSH session.
"""

import os
import select
import shutil
import sys

IS_WINDOWS = os.name == "nt"

if not IS_WINDOWS:
    import termios
    import tty
else:  # pragma: no cover - only exercised when developing on Windows
    import msvcrt

# --- escape sequences ------------------------------------------------------

ESC = "\x1b"
CSI = ESC + "["

CLEAR_SCREEN = CSI + "2J"
CLEAR_LINE = CSI + "2K"
CLEAR_TO_EOL = CSI + "K"
HOME = CSI + "H"
HIDE_CURSOR = CSI + "?25l"
SHOW_CURSOR = CSI + "?25h"
RESET = CSI + "0m"
BOLD = CSI + "1m"
DIM = CSI + "2m"
REVERSE = CSI + "7m"

# The eight basic colors.  The project description asks for these rather than
# the 256-color sequences, so the display looks the same whatever TERM the
# other end reports.
FG = {
    "black": CSI + "30m",
    "red": CSI + "31m",
    "green": CSI + "32m",
    "yellow": CSI + "33m",
    "blue": CSI + "34m",
    "magenta": CSI + "35m",
    "cyan": CSI + "36m",
    "white": CSI + "37m",
}
BG = {
    "black": CSI + "40m",
    "red": CSI + "41m",
    "green": CSI + "42m",
    "yellow": CSI + "43m",
    "blue": CSI + "44m",
    "magenta": CSI + "45m",
    "cyan": CSI + "46m",
    "white": CSI + "47m",
}


def move(row, col):
    """Cursor to a 1-based row and column."""
    return "%s%d;%dH" % (CSI, row, col)


def require_size(min_cols=72, min_rows=20):
    """Exit with a message naming the size we need, rather than drawing junk."""
    size = shutil.get_terminal_size(fallback=(80, 24))
    if size.columns < min_cols or size.lines < min_rows:
        sys.stderr.write(
            "This program needs a terminal of at least %d columns by %d rows.\n"
            "This one reports %d by %d.  Resize the window and run it again.\n"
            % (min_cols, min_rows, size.columns, size.lines)
        )
        raise SystemExit(2)
    return size


def visible_length(text):
    """Length of `text` ignoring ANSI escape sequences."""
    total = 0
    i = 0
    while i < len(text):
        if text[i] == ESC:
            j = i + 1
            if j < len(text) and text[j] == "[":
                j += 1
                while j < len(text) and not text[j].isalpha():
                    j += 1
            i = j + 1
            continue
        total += 1
        i += 1
    return total


def pad(text, width):
    """Pad to `width` *visible* characters.

    str.ljust counts the escape sequences too, so a reversed-video bar padded
    with ljust comes out a few characters short of the window edge.
    """
    text = fit(text, width)
    return text + " " * max(0, width - visible_length(text))


def fit(text, width):
    """Truncate to `width` visible characters, ignoring escape sequences."""
    if visible_length(text) <= width:
        return text
    out = []
    shown = 0
    i = 0
    while i < len(text) and shown < width:
        if text[i] == ESC:
            j = i + 1
            if j < len(text) and text[j] == "[":
                j += 1
                while j < len(text) and not text[j].isalpha():
                    j += 1
            out.append(text[i : j + 1])
            i = j + 1
            continue
        out.append(text[i])
        shown += 1
        i += 1
    out.append(RESET)
    return "".join(out)


# --- keyboard --------------------------------------------------------------


class Keyboard:
    """Non-blocking keystrokes, with the terminal restored on the way out.

    Logical key names: single characters for printable keys, and "UP", "DOWN",
    "LEFT", "RIGHT", "ENTER", "ESC", "BACKSPACE", "TAB" for the rest.  Arrow
    keys arrive as three bytes on POSIX (ESC [ A through ESC [ D) and as a two
    byte prefixed pair on Windows; both are folded into the same names.
    """

    def __init__(self, stream=None):
        self.stream = stream or sys.stdin
        self.interactive = False
        self._fd = None
        self._saved = None
        self._pending = ""

    def __enter__(self):
        try:
            self._fd = self.stream.fileno()
            self.interactive = self.stream.isatty()
        except (AttributeError, OSError, ValueError):
            self.interactive = False
        if self.interactive and not IS_WINDOWS:
            self._saved = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        return self

    def __exit__(self, *exc):
        self.restore()
        return False

    def restore(self):
        """Put the terminal back.  Safe to call more than once."""
        if self._saved is not None and not IS_WINDOWS:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                pass
            self._saved = None

    # -- reading --------------------------------------------------------

    def keys(self):
        """Every key pressed since the last call.  Never blocks."""
        if not self.interactive:
            return []
        return self._keys_windows() if IS_WINDOWS else self._keys_posix()

    def _keys_posix(self):
        chunks = []
        while True:
            try:
                ready, _, _ = select.select([self.stream], [], [], 0)
            except (OSError, ValueError):
                return []
            if not ready:
                break
            try:
                data = os.read(self._fd, 128)
            except (BlockingIOError, OSError):
                break
            if not data:
                break
            chunks.append(data.decode("utf-8", "replace"))
        self._pending += "".join(chunks)
        return self._parse()

    def _keys_windows(self):  # pragma: no cover
        out = []
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # a function or arrow key follows
                if msvcrt.kbhit():
                    code = msvcrt.getwch()
                    out.append(
                        {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}.get(
                            code, "?"
                        )
                    )
                continue
            if ch == "\r":
                out.append("ENTER")
            elif ch == "\x08":
                out.append("BACKSPACE")
            elif ch == "\x03":
                raise KeyboardInterrupt
            elif ch == "\x1b":
                out.append("ESC")
            else:
                out.append(ch)
        return out

    def _parse(self):
        """Pull logical keys off the pending buffer, keeping partial escapes."""
        out = []
        buf = self._pending
        i = 0
        while i < len(buf):
            ch = buf[i]
            if ch == ESC:
                if i + 1 >= len(buf):
                    break  # might be the start of an arrow key, wait for more
                if buf[i + 1] == "[":
                    if i + 2 >= len(buf):
                        break
                    final = buf[i + 2]
                    name = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(
                        final
                    )
                    out.append(name or "ESC")
                    i += 3
                    continue
                out.append("ESC")
                i += 1
                continue
            if ch == "\x03":
                self._pending = buf[i + 1 :]
                raise KeyboardInterrupt
            if ch in ("\r", "\n"):
                out.append("ENTER")
            elif ch in ("\x7f", "\x08"):
                out.append("BACKSPACE")
            elif ch == "\t":
                out.append("TAB")
            else:
                out.append(ch)
            i += 1
        self._pending = buf[i:]
        return out


# --- screen ----------------------------------------------------------------


class Screen:
    """A fixed-height line buffer that writes only what changed."""

    def __init__(self, rows=None, cols=None, stream=None):
        size = shutil.get_terminal_size(fallback=(80, 24))
        self.rows = rows or size.lines
        self.cols = cols or size.columns
        self.stream = stream or sys.stdout
        self._next = [""] * self.rows
        self._shown = [None] * self.rows
        self._started = False

    def __enter__(self):
        self.stream.write(HIDE_CURSOR + CLEAR_SCREEN + HOME)
        self.stream.flush()
        self._started = True
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def stop(self):
        if self._started:
            self.stream.write(RESET + SHOW_CURSOR + move(self.rows, 1) + "\n")
            self.stream.flush()
            self._started = False

    def clear(self):
        self._next = [""] * self.rows

    def put(self, row, text):
        """Queue `text` for a 1-based row.  Out-of-range rows are ignored."""
        if 1 <= row <= self.rows:
            self._next[row - 1] = fit(text, self.cols)

    def put_block(self, row, lines):
        for offset, line in enumerate(lines):
            self.put(row + offset, line)

    def flush(self, force=False):
        """Redraw the lines whose contents differ from what is on screen."""
        out = []
        for index in range(self.rows):
            if force or self._next[index] != self._shown[index]:
                out.append(move(index + 1, 1) + CLEAR_LINE + self._next[index] + RESET)
                self._shown[index] = self._next[index]
        if out:
            self.stream.write("".join(out))
            self.stream.flush()
        return len(out)

    def invalidate(self):
        """Forget what is on screen, so the next flush repaints everything."""
        self._shown = [None] * self.rows
