"""Command-line mode UI: a scrollback area above an input prompt."""
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension

from ..util.paths import shorten_home
from ..util.widgets import WheelScrollControl
from .completer import ShellCompleter
from .lexer import ShellLexer, lex_line

MAX_SCROLLBACK = 2000

# A few extra lines are sliced below the estimated viewport so a one-frame-stale
# height estimate (render_info lags a frame) never blanks the bottom edge.
SCROLL_BUFFER = 5


class ShellView:
    def __init__(self, app):
        self.app = app
        self.lines = []  # list of fragment-lists: [[(style, text), ...], ...]
        # Streamed output not yet terminated by a newline (e.g. a live progress
        # bar). Rendered as the last line and overwritten on carriage returns.
        self._open = ""
        # Output scroll: None = follow the bottom; otherwise the top visible
        # line the viewport is pinned to (set when the user scrolls up).
        self.scroll_top = None

        self.command_buffer = Buffer(
            name="command",
            multiline=False,
            completer=ShellCompleter(app),
            complete_while_typing=False,
            history=InMemoryHistory(),
            accept_handler=app.accept_command,
        )

        # wrap_lines=False so prompt_toolkit honours get_vertical_scroll (it is
        # ignored when line-wrapping); this gives deterministic line scrolling.
        output_control = WheelScrollControl(
            lambda d: self.scroll(d * 3),  # mouse wheel: 3 lines per notch
            text=self._output_text,
            focusable=False,
            get_cursor_position=self._cursor_position,
        )
        self.output_window = Window(
            output_control,
            wrap_lines=False,
            get_vertical_scroll=self._vertical_scroll,
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

    def _cursor_position(self):
        # _output_text feeds only the visible slice, so the top visible line is
        # always content row 0. Pinning the cursor there keeps do_scroll from
        # nudging the (already correct) vertical scroll.
        return Point(0, 0)

    def _vertical_scroll(self, _window):
        # The content is pre-sliced to start at the first visible line, so the
        # window always renders from its top.
        return 0

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

    def _prompt_text(self):
        return [("class:shell.prompt", f"{shorten_home(self.app.cwd)} $ ")]

    @staticmethod
    def _last_segment(text):
        """The visible text after carriage-return overwrites (last non-empty)."""
        for seg in reversed(text.split("\r")):
            if seg != "":
                return seg
        return ""

    def _live_open(self):
        return self._last_segment(self._open) if self._open else ""

    def _output_text(self):
        # Only materialise the lines that can be on screen. Feeding prompt_toolkit
        # the whole scrollback makes every render — and thus every keystroke,
        # since each invalidates — O(total output): it rebuilds, splits and
        # hashes the entire fragment list each frame. Slicing to the viewport
        # keeps that cost bounded by the window height no matter how much has
        # scrolled past.
        top = self._view_top()
        end = top + self._visible_height() + SCROLL_BUFFER
        n = len(self.lines)
        result = []
        for idx in range(top, min(end, n)):
            result.extend(self.lines[idx])
            result.append(("", "\n"))
        live = self._live_open()
        if live and top <= n < end:  # the live line sits at index n (after lines)
            result.extend(to_formatted_text(ANSI(live.expandtabs(4))))
            result.append(("", "\n"))
        return result

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
        line = self._last_segment(raw).expandtabs(4)
        self._push(list(to_formatted_text(ANSI(line))) or [("", "")])

    # -- internal (already-styled) lines -------------------------------------
    def append(self, text, style="class:shell.output"):
        self.flush_output()
        for line in str(text).split("\n"):
            self._push([(style, line)])

    def append_command(self, cmd):
        """Echo an entered command: green prompt + lexer-highlighted command."""
        self.flush_output()
        prompt = f"{shorten_home(self.app.cwd)} $ "
        self._push([("class:shell.prompt", prompt)] + lex_line(cmd))
        self.scroll_top = None  # a new command jumps the view back to the bottom

    def clear(self):
        self.lines = []
        self._open = ""
        self.scroll_top = None
