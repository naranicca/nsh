"""Command-line mode UI: a scrollback area above an input prompt."""
import re

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension

from ..util.width import text_width
from ..util.widgets import WheelScrollControl
from .completer import ShellCompleter
from .lexer import ShellLexer, lex_line
from .prompt import prompt_fragments
from .runner import CommandRunner

MAX_SCROLLBACK = 2000

# CSI escape sequence (e.g. colour SGR "\x1b[31m"); matched so backspace
# resolution can step over escapes without counting them as printable columns.
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# A few extra lines are sliced below the estimated viewport so a one-frame-stale
# height estimate (render_info lags a frame) never blanks the bottom edge.
SCROLL_BUFFER = 5


def _fmt_elapsed(seconds):
    """Compact running time, e.g. ``0.3s`` / ``5s`` / ``2m03s`` / ``1h04m``."""
    if seconds < 1:
        return f"{seconds:.1f}s"  # sub-second (used by the finished-command tint)
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m{sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


class ShellView:
    """One shell session: its own scrollback, input line and running process.

    Several of these are managed as tabs by :class:`~nsh.shell.tabs.ShellTabs`.
    """

    def __init__(self, app):
        self.app = app
        self.lines = []  # list of fragment-lists: [[(style, text), ...], ...]
        # Streamed output not yet terminated by a newline (e.g. a live progress
        # bar). Rendered as the last line and overwritten on carriage returns.
        self._open = ""
        # Output scroll: None = follow the bottom; otherwise the top visible
        # line the viewport is pinned to (set when the user scrolls up).
        self.scroll_top = None
        # this session's process runner, and the tab label (last command's name)
        self.runner = CommandRunner(app, self)
        self.title = "shell"
        # a name set by the user (tab rename); when present it overrides the
        # auto title so a later command doesn't clobber it.
        self.custom_title = None

        self.command_buffer = Buffer(
            name="command",
            multiline=False,
            completer=ShellCompleter(app),
            complete_while_typing=False,
            history=InMemoryHistory(),
            accept_handler=self._accept,
        )

        # wrap_lines=True so long output stays readable. prompt_toolkit ignores
        # get_vertical_scroll while wrapping and scrolls to the cursor instead, so
        # _cursor_position pins the viewport: to the newest line (follow) or to
        # the slice top (scrolled up). See _cursor_position / _output_text.
        output_control = WheelScrollControl(
            lambda d: self.scroll(d * 3),  # mouse wheel: 3 lines per notch
            text=self._output_text,
            focusable=False,
            get_cursor_position=self._cursor_position,
        )
        self.output_window = Window(
            output_control,
            wrap_lines=True,
            height=self._output_height,
            style="class:shell.output",
        )

        # The input line collapses to height 0 while the user is scrolled up
        # (kept in the layout — not removed — so focus and the scroll key
        # bindings stay live to bring it back).
        self.input_window = VSplit(
            [
                Window(FormattedTextControl(self._prompt_text),
                       dont_extend_width=True, height=1),
                Window(BufferControl(buffer=self.command_buffer, lexer=ShellLexer()),
                       height=1),
            ],
            height=self._input_height,
        )
        self.container = HSplit([self.output_window, self.input_window])

    def _accept(self, buff):
        self.app.run_in_shell(self, buff.text)
        return False  # clear the input line

    def busy(self) -> bool:
        return self.runner.is_running()

    # -- auto-grow height -----------------------------------------------------
    def _input_height(self):
        # hidden while scrolled up, visible at the bottom (following)
        return Dimension.exact(0 if self.scroll_top is not None else 1)

    def _output_height(self):
        # Full screen: fill all remaining space. Split: exactly as tall as the
        # output (so the shell grows with its content), capped by the app.
        if self.app.shell_fullscreen():
            return Dimension(min=1)
        return Dimension.exact(self.app.shell_split_output_rows())

    # -- output scrolling -----------------------------------------------------
    def line_count(self):
        """Visible line count, including the live (unterminated) output line."""
        return len(self.lines) + (1 if self._live_open() else 0)

    def _visible_height(self):
        """Output rows shown this frame.

        In split mode the height is deterministic (``shell_split_output_rows``),
        so we must NOT read it from ``render_info``: render_info lags one frame
        behind a height change, and right after a command grows the log that
        stale height makes :meth:`_bottom_top` scroll one row too far, briefly
        exposing the trailing blank row. In fullscreen the height is stable, so
        render_info is fine.
        """
        if self.app.shell_fullscreen():
            ri = self.output_window.render_info
            return ri.window_height if (ri is not None and ri.window_height) else 10
        return max(1, self.app.shell_split_output_rows())

    def _bottom_top(self):
        """The top line index when the very bottom of the log is shown."""
        return max(0, self.line_count() - self._visible_height())

    def _view_top(self):
        """First visible line index into the full log.

        Follows the bottom (``_bottom_top``) unless the user scrolled up, in
        which case the viewport is pinned to ``scroll_top``. This is the slice
        origin used by :meth:`_output_text`.
        """
        if self.scroll_top is None:
            return self._bottom_top()
        return max(0, min(self.scroll_top, max(0, self.line_count() - 1)))

    def _slice(self):
        """``(top, committed, include_live)`` for the currently visible window.

        Only these lines are materialised by :meth:`_output_text`; everything is
        bounded by the window height so a long scrollback never slows rendering.
        """
        top = self._view_top()
        end = top + self._visible_height() + SCROLL_BUFFER
        n = len(self.lines)
        committed = max(0, min(end, n) - top)
        include_live = bool(self._live_open()) and top <= n < end
        return top, committed, include_live

    def _cursor_position(self):
        # While wrapping, prompt_toolkit scrolls to keep the cursor visible. The
        # content is the visible slice only, so pin the cursor to the newest line
        # (follow: bottom of the window) or to the slice top (scrolled up).
        top, committed, include_live = self._slice()
        if self.scroll_top is None:
            count = committed + (1 if include_live else 0)
            return Point(0, max(0, count - 1))
        return Point(0, 0)

    def _page(self):
        ri = self.output_window.render_info
        if ri is not None and ri.window_height:
            return max(1, ri.window_height - 1)
        return 10

    def scroll(self, delta):
        bottom_top = self._bottom_top()
        cur_top = bottom_top if self.scroll_top is None else self.scroll_top
        new_top = max(0, min(cur_top + delta, max(0, self.line_count() - 1)))
        # reaching (or passing) the bottom resumes auto-follow
        self.scroll_top = None if new_top >= bottom_top else new_top
        self.app.invalidate()

    def scroll_to_bottom(self):
        self.scroll_top = None
        self.app.invalidate()

    def _prompt_fragments(self):
        return prompt_fragments(self.app)

    def _prompt_text(self):
        prompt = self._prompt_fragments()
        el = self.runner.elapsed()
        if el is not None:
            # running: prefix the live elapsed time, ticking each second (the app
            # repaints every second). The busy state is also signalled by the
            # tab going orange, so the prompt's $ is left its normal colour.
            # keep the trailing gap outside the badge so its background
            # (none here, but a tint when finished) doesn't bleed past the ]
            return [("class:shell.elapsed", f"[{_fmt_elapsed(el)}]"),
                    ("", " "), *prompt]
        # finished: keep the run time on the prompt until the next command, tinted
        # green on success / red on failure (shown even for sub-second commands).
        result = self.runner.last_result()
        if result is not None:
            duration, rc = result
            style = "class:shell.elapsed.ok" if rc == 0 else "class:shell.elapsed.err"
            return [(style, f"[{_fmt_elapsed(duration)}]"), ("", " "), *prompt]
        return prompt

    @staticmethod
    def _last_segment(text):
        """The visible text after carriage-return overwrites (last non-empty)."""
        for seg in reversed(text.split("\r")):
            if seg != "":
                return seg
        return ""

    @staticmethod
    def _apply_backspace(text):
        r"""Resolve ``\b`` (0x08) the way a terminal does: move the cursor left
        and let later characters overwrite, instead of showing a literal ``^H``.

        SGR colour state is sticky across the cursor moves (re-emitted per run),
        so e.g. ``\x1b[31mab\bX`` keeps X red. Lines without a backspace take a
        fast path and are returned unchanged.
        """
        if "\b" not in text:
            return text
        cells = []   # (sgr_prefix, char) per visible column
        cur = 0
        sgr = ""     # accumulated SGR state, prefixed onto each written char
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == "\b":
                cur = max(0, cur - 1)
                i += 1
            elif ch == "\x1b":
                m = _CSI_RE.match(text, i)
                if not m:
                    i += 1  # lone/again-unrecognised ESC: drop it
                    continue
                seq = m.group()
                if seq.endswith("m"):  # colour/SGR: update sticky state
                    sgr = "" if seq in ("\x1b[m", "\x1b[0m") else sgr + seq
                i = m.end()
            else:  # printable: overwrite at the cursor (or extend the line)
                if cur < len(cells):
                    cells[cur] = (sgr, ch)
                else:
                    cells.append((sgr, ch))
                cur += 1
                i += 1
        out, last = [], None
        for pfx, c in cells:
            if pfx != last:
                out.append("\x1b[0m")  # clear the previous run's colour first
                if pfx:
                    out.append(pfx)
                last = pfx
            out.append(c)
        if last:
            out.append("\x1b[0m")
        return "".join(out)

    def _live_open(self):
        return self._apply_backspace(self._last_segment(self._open)) if self._open else ""

    def _output_text(self):
        # Only materialise the lines that can be on screen. Feeding prompt_toolkit
        # the whole scrollback makes every render — and thus every keystroke,
        # since each invalidates — O(total output): it rebuilds, splits and
        # hashes the entire fragment list each frame. Slicing to the viewport
        # keeps that cost bounded by the window height no matter how much has
        # scrolled past.
        top, committed, include_live = self._slice()
        result = []
        for idx in range(top, top + committed):
            result.extend(self.lines[idx])
            result.append(("", "\n"))
        if include_live:
            result.extend(to_formatted_text(ANSI(self._live_open().expandtabs(4))))
            result.append(("", "\n"))
        return result

    @staticmethod
    def _wrapped_rows(text, cols):
        """Screen rows a single logical line occupies once wrapped to ``cols``."""
        return max(1, -(-text_width(text) // cols))  # ceil, at least one row

    def display_rows(self, cols, limit=None):
        """Total wrapped screen rows for the whole log.

        Used to size the split layout in row units (a long wrapped line must
        reserve more than one row). ``limit`` short-circuits once the running
        total passes it, so the fullscreen check stays O(visible) rather than
        O(scrollback).
        """
        cols = max(1, cols)
        total = 0
        for fragments in self.lines:
            total += self._wrapped_rows("".join(t for _, t in fragments), cols)
            if limit is not None and total > limit:
                return total
        live = self._live_open()
        if live:
            text = "".join(t for _, t in to_formatted_text(ANSI(live.expandtabs(4))))
            total += self._wrapped_rows(text, cols)
        return total

    def _push(self, fragments):
        self.lines.append(fragments)
        if len(self.lines) > MAX_SCROLLBACK:
            self.lines = self.lines[-MAX_SCROLLBACK:]

    # -- streamed command output ---------------------------------------------
    def feed_output(self, text):
        """Feed raw streamed output: LF commits a line, CR overwrites in place."""
        self._open += text
        if "\n" in self._open:
            parts = self._open.split("\n")
            for full in parts[:-1]:
                self._commit_line(full)
            self._open = parts[-1]

    def flush_output(self):
        """Commit any trailing output that never got a final newline."""
        if self._open:
            if self._live_open():
                self._commit_line(self._open)
            self._open = ""

    def _commit_line(self, raw):
        line = self._apply_backspace(self._last_segment(raw)).expandtabs(4)
        self._push(list(to_formatted_text(ANSI(line))) or [("", "")])

    # -- internal (already-styled) lines -------------------------------------
    def append(self, text, style="class:shell.output"):
        self.flush_output()
        for line in str(text).split("\n"):
            self._push([(style, line)])

    def append_command(self, cmd):
        """Echo an entered command: the previous command's run-time badge, the
        green prompt, then the lexer-highlighted command.

        The badge mirrors what the live prompt showed before Enter, so pressing
        Enter scrolls that line up intact instead of dropping the badge. (Read
        ``last_result`` before the caller resets it for the new command.)"""
        self.flush_output()
        line = []
        result = self.runner.last_result()
        if result is not None:
            duration, rc = result
            style = "class:shell.elapsed.ok" if rc == 0 else "class:shell.elapsed.err"
            line += [(style, f"[{_fmt_elapsed(duration)}]"), ("", " ")]
        line += self._prompt_fragments() + lex_line(cmd)
        self._push(line)
        self.scroll_top = None  # a new command jumps the view back to the bottom

    def clear(self):
        self.lines = []
        self._open = ""
        self.scroll_top = None
