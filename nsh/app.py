"""The nsh application: layout, the two modes, and central dispatch."""
import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu

from . import config
from .explorer import git
from .explorer.preview import PreviewView
from .explorer.view import ExplorerView
from .search.view import SearchView
from .shell.runner import CommandRunner
from .shell.view import ShellView
from .util.menu import Menu
from .util.paths import shorten_home
from .util.width import text_width

EXPLORER = "explorer"
SHELL = "shell"
SEARCH = "search"

# Once the shell output would shrink the explorer below this many rows, the
# shell takes over the whole screen.
SHELL_MIN_EXPLORER = 5


class NshApp:
    def __init__(self, start_mode=None, query="", picker=False):
        self.cwd = Path.cwd().resolve()
        self.mode = EXPLORER
        self.message = ""
        self.git_status = git.GitStatus()
        self._git_task = None

        # search-mode startup / result plumbing
        self._start_mode = start_mode
        self._initial_query = query
        self._pending_query = ""
        self.picker = picker
        self.search_result = None

        self.runner = CommandRunner(self)
        self.explorer = ExplorerView(self)
        self.shell = ShellView(self)
        self.preview = PreviewView(self)
        self.show_preview = True
        self.search = SearchView(self)

        # one-line input overlay (commit message, rename, new file/folder)
        self._prompt_active = False
        self._prompt_label = ""
        self._prompt_callback = None
        self.prompt_buffer = Buffer(multiline=False, accept_handler=self._prompt_accept)

        # yes/no confirmation overlay (used before a destructive delete)
        self._confirm_active = False
        self._confirm_label = ""
        self._confirm_callback = None
        self._confirm_control = None

        # popup action menu (Tab in the explorer)
        self.menu = Menu(self._menu_closed)

        self.application = self._build_application()

    # -- layout ---------------------------------------------------------------
    def _build_application(self):
        prompt_open = Condition(lambda: self._prompt_active)
        confirm_open = Condition(lambda: self._confirm_active)
        menu_open = Condition(lambda: self.menu.active)
        overlay_open = prompt_open | confirm_open | menu_open

        kb = KeyBindings()

        @kb.add("escape", filter=~overlay_open)
        def _(event):
            buff = self.shell.command_buffer
            if self.mode == SEARCH:
                self.cancel_search()
            elif self.mode == SHELL:
                if buff.complete_state:
                    buff.cancel_completion()
                else:
                    self.switch_mode(EXPLORER)
            else:  # EXPLORER: clear any multi-selection
                self.explorer.clear_selection()

        @kb.add("escape", filter=prompt_open)
        def _(event):
            self.close_prompt()

        @kb.add("c-q")
        def _(event):
            self.exit()

        # shell output scrolling — global (not on the input buffer) so it keeps
        # working even while the command line is hidden during a scroll-up
        shell_mode = Condition(lambda: self.mode == SHELL)

        @kb.add("pageup", filter=shell_mode)
        def _(event):
            self.shell.scroll(-self.shell._page())

        @kb.add("pagedown", filter=shell_mode)
        def _(event):
            self.shell.scroll(self.shell._page())

        @kb.add("c-end", filter=shell_mode)
        def _(event):
            self.shell.scroll_to_bottom()

        # confirmation overlay keys (active only while it owns focus)
        confirm_kb = KeyBindings()

        @confirm_kb.add("y")
        @confirm_kb.add("Y")
        def _(event):
            self._resolve_confirm(True)

        @confirm_kb.add("n")
        @confirm_kb.add("N")
        @confirm_kb.add("escape")
        @confirm_kb.add("c-c")
        def _(event):
            self._resolve_confirm(False)

        self._confirm_control = FormattedTextControl(
            lambda: [
                ("class:dialog.label", f" {self._confirm_label} "),
                ("class:dialog", "   "),
                ("class:statusbar.key", " y "), ("class:dialog", " yes  "),
                ("class:statusbar.key", " n "), ("class:dialog", " no "),
            ],
            focusable=True,
            show_cursor=False,
            key_bindings=confirm_kb,
        )

        self._explorer_split = VSplit(
            [
                self.explorer.window,
                Window(width=1, char="│", style="class:preview.border"),
                self.preview.window,
            ]
        )

        # the explorer area (with or without the preview pane), reused both in
        # explorer mode and on top of the shell so the listing stays visible
        explorer_area = DynamicContainer(
            lambda: self._explorer_split
            if (self.show_preview and self._wide_enough())
            else self.explorer.window
        )

        # shell mode keeps the explorer on top, command line + output below
        self._shell_split = HSplit(
            [
                explorer_area,
                Window(height=1, char="─", style="class:preview.border"),
                self.shell.container,
            ]
        )

        def _body():
            if self.mode == SEARCH:
                return self.search.container
            if self.mode == SHELL:
                # grow with output, then take the whole screen at the cap
                return self.shell.container if self.shell_fullscreen() else self._shell_split
            return explorer_area

        body = DynamicContainer(_body)

        prompt_overlay = ConditionalContainer(
            HSplit(
                [
                    Window(
                        FormattedTextControl(
                            lambda: [("class:dialog.label", f" {self._prompt_label} ")]
                        ),
                        height=1,
                    ),
                    Window(
                        BufferControl(buffer=self.prompt_buffer),
                        height=1,
                        style="class:dialog.input",
                    ),
                ],
                style="class:dialog",
            ),
            filter=prompt_open,
        )

        confirm_overlay = ConditionalContainer(
            Window(self._confirm_control, height=1, style="class:dialog"),
            filter=confirm_open,
        )

        root = FloatContainer(
            content=HSplit(
                [
                    Window(FormattedTextControl(self._title_text), height=1,
                           style="class:titlebar"),
                    body,
                    Window(FormattedTextControl(self._status_text), height=1,
                           style="class:statusbar"),
                ]
            ),
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=16, scroll_offset=1),
                ),
                Float(top=2, left=4, right=4, content=prompt_overlay),
                Float(top=2, left=4, right=4, content=confirm_overlay),
                Float(top=2, left=4, content=self.menu.container),
            ],
        )

        return Application(
            layout=Layout(root, focused_element=self.explorer.control),
            key_bindings=merge_key_bindings([load_key_bindings(), kb]),
            style=config.STYLE,
            full_screen=True,
            mouse_support=True,  # enables mouse-wheel scrolling of the log/list
            refresh_interval=1.0,  # keep the title-bar clock ticking
        )

    # -- title / status -------------------------------------------------------
    def _fill(self, segs, style):
        try:
            cols = get_app().output.get_size().columns
        except Exception:
            cols = 80
        used = sum(text_width(t) for _, t in segs)
        if used < cols:
            segs.append((style, " " * (cols - used)))
        return segs

    def _fill_with_right(self, left, fill_style, right):
        """Pad ``left`` so that ``right`` sits flush against the right edge."""
        try:
            cols = get_app().output.get_size().columns
        except Exception:
            cols = 80
        used = sum(text_width(t) for _, t in left)
        rused = sum(text_width(t) for _, t in right)
        pad = cols - used - rused
        if pad > 0:
            left.append((fill_style, " " * pad))
        return left + right

    def _title_text(self):
        segs = [
            ("class:titlebar.mode", f" {self.mode.upper()} "),
            ("class:titlebar", " "),
            ("class:titlebar.path", shorten_home(self.cwd)),
        ]
        if self.git_status.is_repo and self.git_status.branch:
            segs.append(("class:titlebar", "  on "))
            segs.append(("class:titlebar.branch", f"⎇ {self.git_status.branch}"))
        if self.mode == EXPLORER and self.explorer.selected:
            segs.append(("class:titlebar", "   "))
            segs.append(("class:titlebar.sel", f"● {len(self.explorer.selected)} selected"))
        clock = [("class:titlebar.clock", f" {datetime.now().strftime('%H:%M:%S')} ")]
        return self._fill_with_right(segs, "class:titlebar", clock)

    def _status_text(self):
        if self.mode == EXPLORER:
            hints = [
                ("↑↓", "move"), ("↵", "open"), ("Space", "select"),
                ("Tab", "actions"), ("/", "find"), (":", "cmd"), ("q", "quit"),
            ]
        elif self.mode == SEARCH:
            hints = [
                ("type", "filter"), ("↑↓", "move"), ("↵", "select"),
                ("ESC", "cancel"),
            ]
        else:
            hints = [
                ("Tab", "complete"), ("↵", "run"), ("↑↓", "history"),
                ("PgUp/PgDn", "scroll"), ("ESC", "explorer"),
            ]
        segs = []
        for key, label in hints:
            segs.append(("class:statusbar.key", f" {key} "))
            segs.append(("class:statusbar", f"{label} "))
        if self.message:
            segs.append(("class:statusbar.msg", f" {self.message}"))
        return self._fill(segs, "class:statusbar")

    # -- modes ----------------------------------------------------------------
    def toggle_mode(self):
        self.switch_mode(EXPLORER if self.mode == SHELL else SHELL)

    def switch_mode(self, mode):
        self.mode = mode
        if mode == SHELL:
            self.application.layout.focus(self.shell.command_buffer)
        elif mode == SEARCH:
            self.search.start(self._pending_query)
            self._pending_query = ""
            self.application.layout.focus(self.search.query_buffer)
        else:
            self.application.layout.focus(self.explorer.control)
        self.invalidate()

    # -- fuzzy search ---------------------------------------------------------
    def enter_search(self, query=""):
        self._pending_query = query
        self.switch_mode(SEARCH)

    def search_select(self, path):
        if self.picker:
            self.search_result = str(path)
            self.exit()
            return
        if path.is_dir():
            self.set_cwd(path)
        else:
            self.set_cwd(path.parent)
            self.explorer.refresh_listing(select_name=path.name)
        self.switch_mode(EXPLORER)

    def cancel_search(self):
        if self.picker:
            self.search_result = None
            self.exit()
            return
        self.switch_mode(EXPLORER)

    def toggle_preview(self):
        self.show_preview = not self.show_preview
        self.invalidate()

    def _wide_enough(self):
        try:
            return get_app().output.get_size().columns >= 80
        except Exception:
            return True

    # -- shell auto-grow ------------------------------------------------------
    def _shell_cap(self):
        """Max output rows the shell may use while still sharing with explorer."""
        try:
            rows = get_app().output.get_size().rows
        except Exception:
            rows = 24
        body = max(1, rows - 2)  # minus the title and status bars
        # leave room for the input line (1), the separator (1) and the explorer
        return max(1, body - 2 - SHELL_MIN_EXPLORER)

    def shell_fullscreen(self):
        """True once the output no longer fits the shared (split) layout."""
        return self.shell.line_count() > self._shell_cap()

    def shell_split_output_rows(self):
        """Output rows in split mode: grows with content up to the cap."""
        return max(0, min(self.shell.line_count(), self._shell_cap()))

    # -- command line ---------------------------------------------------------
    def accept_command(self, buff):
        cmd = buff.text
        if not cmd.strip():
            return False
        self.shell.append_command(cmd)
        if not self._handle_builtin(cmd):
            asyncio.ensure_future(self._exec(cmd))
        return False  # clear the input line

    def _handle_builtin(self, cmd):
        stripped = cmd.strip()
        if stripped in ("exit", "quit"):
            self.exit()
            return True
        if stripped in ("clear", "cls"):
            self.shell.clear()
            return True
        if stripped == "cd" or stripped.startswith("cd "):
            target = stripped[2:].strip() or "~"
            path = Path(os.path.expanduser(target))
            if not path.is_absolute():
                path = self.cwd / path
            try:
                path = path.resolve()
            except OSError:
                pass
            if path.is_dir():
                self.set_cwd(path)
            else:
                self.shell.append(f"cd: no such directory: {target}", "class:shell.error")
            return True
        return False

    async def _exec(self, cmd):
        try:
            if self.runner.is_interactive(cmd):
                await self.runner.run_in_term(cmd)
            else:
                await self.runner.run(cmd)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.shell.append(f"nsh: {exc}", "class:shell.error")
        self.invalidate()

    def open_file(self, path):
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
        if editor:
            asyncio.ensure_future(self.runner.run_in_term(f'{editor} "{path}"'))
            return
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            self.set_message(f"cannot open: {exc}")

    # -- directory / git ------------------------------------------------------
    def set_cwd(self, path):
        path = Path(path)
        try:
            os.chdir(path)
        except OSError as exc:
            self.set_message(f"cannot enter: {exc}")
            return
        self.cwd = path.resolve()
        self.explorer.cursor = 0
        self.explorer.selected.clear()
        self.explorer.load()
        self.preview.clear()
        self.message = ""
        self.schedule_git()
        self.invalidate()

    def schedule_git(self):
        if self._git_task and not self._git_task.done():
            self._git_task.cancel()
        self.git_status = git.GitStatus()
        self._git_task = asyncio.ensure_future(self._git_worker(self.cwd))

    async def _git_worker(self, path):
        status = await git.query(path)
        if path == self.cwd:  # ignore results for a directory we already left
            self.git_status = status
            self.invalidate()

    async def refresh_git(self):
        self.git_status = await git.query(self.cwd)
        self.invalidate()

    # -- input overlay --------------------------------------------------------
    def _restore_focus(self):
        if self.mode == SHELL:
            self.application.layout.focus(self.shell.command_buffer)
        else:
            self.application.layout.focus(self.explorer.control)

    def ask(self, label, callback, default=""):
        self._prompt_label = label
        self._prompt_callback = callback
        self._prompt_active = True
        self.prompt_buffer.text = default
        self.prompt_buffer.cursor_position = len(default)
        self.application.layout.focus(self.prompt_buffer)
        self.invalidate()

    def close_prompt(self):
        self._prompt_active = False
        self._prompt_callback = None
        self._restore_focus()
        self.invalidate()

    def _prompt_accept(self, buff):
        callback = self._prompt_callback
        text = buff.text
        self.close_prompt()
        if callback:
            callback(text)
        return False

    # -- confirmation overlay -------------------------------------------------
    def confirm(self, label, callback):
        """Show a yes/no overlay; ``callback(True|False)`` on resolve."""
        self._confirm_label = label
        self._confirm_callback = callback
        self._confirm_active = True
        self.application.layout.focus(self._confirm_control)
        self.invalidate()

    def _resolve_confirm(self, ok):
        callback = self._confirm_callback
        self._confirm_active = False
        self._confirm_callback = None
        self._restore_focus()
        if callback:
            callback(ok)
        self.invalidate()

    # -- action menu ----------------------------------------------------------
    def open_menu(self, title, items):
        self.menu.open(title, items)
        self.application.layout.focus(self.menu.control)
        self.invalidate()

    def _menu_closed(self):
        self._restore_focus()
        self.invalidate()

    # -- misc -----------------------------------------------------------------
    def set_message(self, message):
        self.message = message
        self.invalidate()

    def invalidate(self):
        try:
            self.application.invalidate()
        except Exception:
            pass

    def exit(self):
        try:
            self.application.exit()
        except Exception:
            pass

    async def run_async(self):
        self.explorer.load()
        self.schedule_git()
        if self._start_mode == SHELL:
            self.switch_mode(SHELL)
        elif self._start_mode == SEARCH:
            self.enter_search(self._initial_query)
        await self.application.run_async()
        return self.search_result
