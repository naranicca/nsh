"""Command-line mode UI: a scrollback area above an input prompt."""
import re

from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import Margin

from ..util.width import char_width, text_width
from ..util.widgets import WheelScrollControl
from .completer import ShellCompleter
from .lexer import ShellLexer, lex_line
from .prompt import prompt_fragments
from .runner import CommandRunner


def _scrollback_limit(app):
    try:
        return max(1, min(100000, int(app.settings.get(
            "scrollback_lines", "2000"))))
    except (AttributeError, TypeError, ValueError):
        return 2000

# CSI escape sequence (e.g. colour SGR "\x1b[31m"); matched so backspace
# resolution can step over escapes without counting them as printable columns.
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# A few extra lines are sliced below the estimated viewport so a one-frame-stale
# height estimate (render_info lags a frame) never blanks the bottom edge.
SCROLL_BUFFER = 5


class _CommandScrollMargin(Margin):
    """Fixed one-cell marker when command text is clipped on either side."""

    def __init__(self, shell_view, side):
        self.shell_view = shell_view
        self.side = side

    def get_width(self, get_ui_content):
        return 1

    def create_margin(self, window_render_info, width, height):
        window = self.shell_view.command_window
        scroll = getattr(window, "horizontal_scroll", 0)
        if self.side == "left":
            clipped = scroll > 0
        else:
            command_width = text_width(self.shell_view.command_buffer.text)
            clipped = command_width > scroll + window_render_info.window_width
        marker = "⋯" if clipped else " "
        return [("class:shell.prompt.dim", marker)]


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
        # commands the user chose to queue while an earlier one was still
        # running; they run one after another in this same tab (see app.run_in_shell)
        self.pending = []
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
            # typing while scrolled up jumps the view back to the bottom, so the
            # (collapsed) input line reappears and you can see what you type
            on_text_changed=self._on_command_changed,
            on_cursor_position_changed=self._on_command_cursor_changed,
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
        self.command_window = Window(
            BufferControl(
                buffer=self.command_buffer, lexer=ShellLexer(),
                # Keep the completion menu anchored sensibly on a single, non-
                # wrapping input line (see _menu_position).
                menu_position=self._menu_position),
            left_margins=[_CommandScrollMargin(self, "left")],
            right_margins=[_CommandScrollMargin(self, "right")],
            height=1,
        )
        # Keep the prompt in a separate window: only the command buffer scrolls
        # horizontally, so the elapsed-time badge and `$ ` remain visible.
        self.input_window = VSplit(
            [
                Window(FormattedTextControl(self._prompt_text),
                       dont_extend_width=True, height=1),
                self.command_window,
            ],
            height=self._input_height,
        )
        # commands waiting to run in this tab, listed in grey just above the
        # prompt (one row each; collapses to nothing when the queue is empty)
        self.queue_window = Window(
            FormattedTextControl(self._queue_text),
            height=self._queue_height,
            style="class:shell.queued",
        )
        self.container = HSplit(
            [self.output_window, self.queue_window, self.input_window])

    def _menu_position(self):
        """Where the completion menu attaches on the single-line, non-wrapping
        input.

        prompt_toolkit's default anchors the menu at the start of the completion
        (a fixed document index), which is stable while you cycle candidates — so
        we keep it (return ``None``) whenever that point is still on screen.

        But completing a long name scrolls that start off the left edge; the
        renderer then can't map its (off-screen) column to a screen position,
        falls back to (0, 0), and throws the menu to the top of the screen. Only
        then do we re-anchor at the cursor, which the input always keeps visible.
        """
        buff = self.command_buffer
        state = buff.complete_state
        if not state:
            return None
        anchor = min(buff.cursor_position, state.original_document.cursor_position)
        anchor_col = text_width(buff.text[:anchor])
        # the Window tracks its own left scroll (display column of the first
        # visible char); when the stable anchor falls left of it, it's off-screen
        h_scroll = getattr(self.command_window, "horizontal_scroll", 0)
        if anchor_col < h_scroll:
            return buff.cursor_position  # the stable anchor scrolled off-screen
        return None  # on screen: keep prompt_toolkit's stable default

    def _accept(self, buff):
        self.app.run_in_shell(self, buff.text)
        # The accepted line may have scrolled far to the right. Its replacement
        # is the much shorter dim queue prompt, but that prompt change does not
        # itself fire a buffer/cursor event, so prompt_toolkit would retain the
        # old offset and render the prompt off-screen until another keypress.
        self.command_window.horizontal_scroll = 0
        return False  # clear the input line

    def _on_command_changed(self, _buff):
        # follow the bottom as soon as the user starts typing (no-op when already
        # following, so it doesn't fight a deliberate scroll-up that has no input)
        if self.scroll_top is not None:
            self.scroll_to_bottom()
        self._reset_stale_input_scroll(_buff)

    def _on_command_cursor_changed(self, buff):
        self._reset_stale_input_scroll(buff)

    def _reset_stale_input_scroll(self, buff):
        """Discard horizontal scroll left behind by a longer history entry.

        prompt_toolkit normally keeps this value in step with the cursor, but a
        history replacement can shorten the document without reducing the old
        scroll offset. The new command then sits to the left of the viewport and
        appears blank until the user moves the cursor. Only reset that invalid
        case; normal scrolling within a genuinely long command is untouched.
        """
        window = getattr(self, "command_window", None)
        if window is None:
            return
        cursor_col = text_width(buff.text[:buff.cursor_position])
        horizontal_scroll = getattr(window, "horizontal_scroll", 0)
        if horizontal_scroll > cursor_col:
            window.horizontal_scroll = 0

    def busy(self) -> bool:
        return self.runner.is_running()

    def errored(self) -> bool:
        """True when this tab's last finished command exited non-zero — the cue
        to tint the tab red (matching the prompt's failed run-time badge). A
        currently-running command isn't counted; busy takes precedence."""
        result = self.runner.last_result()
        return result is not None and result[1] not in (0, None)

    # -- pending queue --------------------------------------------------------
    def remove_last_pending(self):
        """Remove and return the newest queued command, leaving the active job alone."""
        if not self.pending:
            return None
        command = self.pending.pop()
        self.app.invalidate()
        return command

    def _queue_height(self):
        # one row per queued command; collapses away (and stays hidden while the
        # user is scrolled up, like the prompt) when there's nothing waiting
        if self.scroll_top is not None:
            return Dimension.exact(0)
        return Dimension.exact(len(self.pending))

    def _queue_text(self):
        # the running command's elapsed time prefixes the first queued command
        # (the one that runs the moment it finishes); later rows are indented to
        # keep their `$` aligned under the first one
        el = self.runner.elapsed()
        badge = f"[{_fmt_elapsed(el)}] " if el is not None else ""
        pad = " " * text_width(badge)
        out = []
        for i, cmd in enumerate(self.pending):
            if i:
                out.append(("", "\n"))
            if i == 0 and badge:
                out.append(("class:shell.elapsed", badge.rstrip()))
                out.append(("class:shell.queued", " "))
            elif badge:
                out.append(("class:shell.queued", pad))
            out.append(("class:shell.queued", f"$ {cmd}"))
        return out

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

    @staticmethod
    def _right_fit_fragments(fragments, width):
        """Keep the styled right-hand tail, including the final ``$``."""
        if sum(text_width(text) for _, text in fragments) <= width:
            return fragments
        if width <= 1:
            # The normal prompt's `$` uses the terminal's default foreground;
            # clipping must not replace that with the grey overflow style.
            style = fragments[-1][0] if fragments else ""
            return [(style, "$")]
        budget = width - 1
        kept = []
        for style, text in reversed(fragments):
            chars = []
            for ch in reversed(text):
                cells = char_width(ch)
                if cells > budget:
                    break
                chars.append(ch)
                budget -= cells
            if chars:
                kept.append((style, "".join(reversed(chars))))
            if budget <= 0:
                break
        kept.reverse()
        return [("class:shell.prompt.dim", "…"), *kept]

    def _prompt_width_cap(self, minimum=1):
        """Prompt width left after giving the typed command all space it needs.

        A short command lets the prompt use up to half the terminal. As the
        command grows, cwd / branch are progressively removed until only the
        caller-supplied minimum (elapsed badge plus ``$ ``, when present) stays.
        """
        try:
            columns = get_app().output.get_size().columns
        except Exception:
            columns = 80
        command_width = text_width(self.command_buffer.text) + 1  # cursor cell
        available = columns - command_width
        return max(minimum, min(columns // 2, available))

    def _prompt_text(self):
        prompt = self._prompt_fragments()
        # The command window's fixed left marker margin supplies the single
        # blank after `$` when nothing is clipped (and becomes `⋯` when the
        # command's front is clipped). Do not leave a second blank here.
        if prompt and prompt[-1][1].endswith("$ "):
            prompt[-1] = (prompt[-1][0], prompt[-1][1][:-1])
        el = self.runner.elapsed()
        if el is not None:
            # The previous command has not finished, so this is a queue-entry
            # prompt: omit cwd / branch and show only a dim `$`.
            dim_dollar = [("class:shell.prompt.dim", "$")]
            # the live elapsed time (ticking each second, as the app repaints)
            # prefixes the prompt — unless commands are queued, in which case it
            # moves up to sit before the first queued command instead. Pad by
            # that badge's width so this `$` stays aligned with the queue above.
            if self.pending:
                badge = f"[{_fmt_elapsed(el)}] "
                return [
                    ("class:shell.queued", " " * text_width(badge)),
                    *dim_dollar,
                ]
            # keep the trailing gap outside the badge so its background
            # (none here, but a tint when finished) doesn't bleed past the ]
            return [
                ("class:shell.elapsed", f"[{_fmt_elapsed(el)}]"),
                ("", " "), *dim_dollar,
            ]
        # finished: keep the run time on the prompt until the next command, tinted
        # green on success / red on failure (shown even for sub-second commands).
        result = self.runner.last_result()
        if result is not None:
            duration, rc = result
            style = "class:shell.elapsed.ok" if rc == 0 else "class:shell.elapsed.err"
            prefix = [(style, f"[{_fmt_elapsed(duration)}]"), ("", " ")]
            prefix_width = sum(text_width(text) for _, text in prefix)
            cap = self._prompt_width_cap(prefix_width + 1)
            remaining = max(1, cap - prefix_width)
            return prefix + self._right_fit_fragments(prompt, remaining)
        return self._right_fit_fragments(prompt, self._prompt_width_cap())

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
        self.trim_scrollback()

    def trim_scrollback(self):
        """Apply the live Preferences limit while preserving scroll position."""
        excess = len(self.lines) - _scrollback_limit(self.app)
        if excess <= 0:
            return
        del self.lines[:excess]
        if self.scroll_top is not None:
            self.scroll_top = max(0, self.scroll_top - excess)

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
