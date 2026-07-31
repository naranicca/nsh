"""Command shell backed by the SSH connection used by the remote file pane."""
import asyncio
import posixpath
import shlex

from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.margins import Margin

from ..util.aio import run_in_thread
from ..util.width import char_width, text_width


class _CommandScrollMargin(Margin):
    """Match the local shell's fixed left/right clipping indicators."""

    def __init__(self, shell, side):
        self.shell, self.side = shell, side

    def get_width(self, get_ui_content):
        return 1

    def create_margin(self, window_render_info, width, height):
        scroll = getattr(self.shell.command_window, "horizontal_scroll", 0)
        if self.side == "left":
            clipped = scroll > 0
        else:
            command_width = text_width(self.shell.command_buffer.text)
            clipped = command_width > scroll + window_render_info.window_width
        return [("class:shell.prompt.dim", "⋯" if clipped else " ")]


class RemoteShellView:
    def __init__(self, app):
        self.app = app
        self.lines = []
        self.busy = False
        self.command_buffer = Buffer(
            name="remote-command", multiline=False,
            history=InMemoryHistory(), accept_handler=self._accept,
            on_text_changed=self._reset_stale_input_scroll,
            on_cursor_position_changed=self._reset_stale_input_scroll)
        self.buffer = self.command_buffer  # app focus compatibility
        self.output = Window(
            FormattedTextControl(
                self._output_text,
                get_cursor_position=lambda: Point(0, max(0, len(self.lines) - 1))),
            wrap_lines=True,
            style="class:shell.output")
        self.prompt = Window(
            FormattedTextControl(self._prompt_text),
            dont_extend_width=True, height=1)
        self.command_window = Window(
            BufferControl(self.command_buffer),
            left_margins=[_CommandScrollMargin(self, "left")],
            right_margins=[_CommandScrollMargin(self, "right")],
            height=1)
        self.input = self.command_window
        self.container = HSplit([
            self.output,
            Window(height=1, char="─", style="class:preview.border"),
            VSplit([self.prompt, self.input], height=1),
        ])

    def _prompt_text(self):
        view = self.app.networkview
        location = view.location if view.connected else "ssh"
        style = "class:shell.prompt.dim" if self.busy else "class:explorer.dir"
        fragments = [(style, f"{location} $")]
        return self._right_fit_fragments(fragments, self._prompt_width_cap())

    @staticmethod
    def _right_fit_fragments(fragments, width):
        if sum(text_width(text) for _style, text in fragments) <= width:
            return fragments
        if width <= 1:
            return [("class:shell.prompt.dim", "$")]
        budget = width - 1
        kept = []
        for style, value in reversed(fragments):
            chars = []
            for char in reversed(value):
                cells = char_width(char)
                if cells > budget:
                    break
                chars.append(char)
                budget -= cells
            if chars:
                kept.append((style, "".join(reversed(chars))))
            if budget <= 0:
                break
        kept.reverse()
        return [("class:shell.prompt.dim", "…"), *kept]

    def _prompt_width_cap(self):
        try:
            columns = get_app().output.get_size().columns
        except Exception:
            columns = 80
        command_width = text_width(self.command_buffer.text) + 1
        return max(1, min(columns // 2, columns - command_width))

    def _reset_stale_input_scroll(self, buffer):
        window = getattr(self, "command_window", None)
        if window is None:
            return
        cursor_col = text_width(buffer.text[:buffer.cursor_position])
        if getattr(window, "horizontal_scroll", 0) > cursor_col:
            window.horizontal_scroll = 0

    def _output_text(self):
        out = []
        for line in self.lines[-2000:]:
            out.extend(line)
            out.append(("", "\n"))
        return out

    def append(self, text, style="class:shell.output"):
        for line in str(text).splitlines() or [""]:
            if "\x1b[" in line:
                self.lines.append(list(to_formatted_text(ANSI(line))))
            else:
                self.lines.append([(style, line)])

    def _accept(self, buffer):
        command = buffer.text.strip()
        if command:
            self.run(command)
        self.command_window.horizontal_scroll = 0
        return False

    def run(self, command):
        if self.busy:
            self.app.set_message("wait for the remote command to finish")
            return
        if command in ("exit", "quit"):
            self.app.switch_mode("network")
            return
        if command in ("clear", "cls"):
            self.lines.clear()
            self.app.invalidate()
            return
        if command == "cd" or command.startswith("cd "):
            target = command[2:].strip() or getattr(
                self.app.networkview.backend, "home", "/")
            self._change_directory(target)
            return
        self.lines.append([
            ("class:explorer.dir", f"{self.app.networkview.location} $ "),
            ("class:shell.output", command),
        ])
        self.busy = True

        async def do():
            try:
                backend = self.app.networkview.backend
                output, error, status = await run_in_thread(
                    self.app.networkview._backend_call,
                    backend.execute, self.app.networkview.path, command)
                if output:
                    self.append(output)
                if error:
                    self.append(error, "class:shell.error")
                if status:
                    self.app.set_message(f"remote command exited {status}")
            except Exception as exc:
                self.append(f"ssh: {exc}", "class:shell.error")
            finally:
                self.busy = False
                self.app.invalidate()
        asyncio.ensure_future(do())

    def _change_directory(self, target):
        view = self.app.networkview
        if target == "~":
            candidate = getattr(view.backend, "home", "/")
        elif target.startswith("/"):
            candidate = target
        else:
            candidate = posixpath.join(view.path, target)
        candidate = posixpath.normpath(candidate)
        self.busy = True

        async def do():
            old = view.path
            try:
                # Let the remote shell resolve permissions and symlinks, then
                # use that canonical directory in both shell and file views.
                output, error, status = await run_in_thread(
                    view._backend_call, view.backend.execute,
                    view.path, f"cd -- {shlex.quote(candidate)} && pwd")
                if status:
                    self.append(error or f"cd: {target}", "class:shell.error")
                    return
                view.path = output.strip().splitlines()[-1]
                await view._load()
            except Exception as exc:
                view.path = old
                self.append(f"cd: {exc}", "class:shell.error")
            finally:
                self.busy = False
                self.app.invalidate()
        asyncio.ensure_future(do())
