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

    def _bottom_top(self):
        """The top line index when the very bottom of the log is shown."""
        ri = self.output_window.render_info
        height = ri.window_height if (ri is not None and ri.window_height) else 10
        return max(0, self.line_count() - height)

    def _cursor_position(self):
        # Keep the "cursor" on the top visible line so do_scroll leaves the
        # preferred vertical scroll (above) untouched.
        last = max(0, self.line_count() - 1)
        if self.scroll_top is None:
            return Point(0, last)
        return Point(0, min(self.scroll_top, last))

    def _vertical_scroll(self, _window):
        if self.scroll_top is None:
            return self._bottom_top()
        return max(0, min(self.scroll_top, max(0, self.line_count() - 1)))

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
        result = []
        for fragments in self.lines:
            result.extend(fragments)
            result.append(("", "\n"))
        live = self._live_open()
        if live:
            result.extend(to_formatted_text(ANSI(live)))
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
        line = self._last_segment(raw)
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
