"""The nsh application: layout, the two modes, and central dispatch."""
import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.layout.containers import (
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu

from . import config
from .explorer import git
from .explorer.preview import PreviewView
from .explorer.view import ExplorerView
from .search.view import SearchView
from .shell.runner import CommandRunner
from .shell.view import ShellView
from .util.bookmarks import Bookmarks
from .util.dialog import ConfirmDialog, InputDialog
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
        self.git_status = git.GitStatus()
        self._git_task = None

        # user configuration (~/.config/nsh/nshrc): colours + explorer keys
        config.ensure_default_config()
        color_overrides, key_overrides, cfg_warning = config.load_user_config()
        self.keys = {**config.DEFAULT_KEYS, **key_overrides}
        self.style = config.build_style(color_overrides)
        self.message = cfg_warning or ""

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

        # popup action menu (Tab in the explorer)
        self.menu = Menu(self._menu_closed)
        self.bookmarks = Bookmarks()
        # centered modal dialogs: text input (rename/new) and yes/no confirm
        self.dialog = InputDialog(self._dialog_closed)
        self.confirm_dialog = ConfirmDialog(self._dialog_closed)

        self.application = self._build_application()

    # -- layout ---------------------------------------------------------------
    def _build_application(self):
        confirm_open = Condition(lambda: self.confirm_dialog.active)
        menu_open = Condition(lambda: self.menu.active)
        dialog_open = Condition(lambda: self.dialog.active)
        overlay_open = confirm_open | menu_open | dialog_open

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

        @kb.add("c-q")
        def _(event):
            self.exit()

        @kb.add("c-c")
        def _(event):
            # Ctrl-C stops the running command (and its children) but never quits
            # nsh; with nothing running it just clears the command line.
            if self.runner.interrupt():
                self.shell.append("^C")
            elif self.mode == SHELL:
                self.shell.command_buffer.reset()

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

        # Ctrl-D on an empty command line quits nsh (shell convention); with text
        # present the filter is false, so the default delete-char still applies.
        shell_line_empty = shell_mode & Condition(
            lambda: not self.shell.command_buffer.text
        )

        @kb.add("c-d", filter=shell_line_empty)
        def _(event):
            self.exit()

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
                # row 1 = directly under the title bar (row 0); left=1 aligns the
                # menu with the "nsh" label
                Float(top=1, left=1, content=self.menu.container),
                # unpositioned Floats are centered on screen
                Float(content=self.dialog.container),
                Float(content=self.confirm_dialog.container),
            ],
        )

        application = Application(
            layout=Layout(root, focused_element=self.explorer.control),
            key_bindings=merge_key_bindings([load_key_bindings(), kb]),
            style=self.style,
            full_screen=True,
            mouse_support=True,  # enables mouse-wheel scrolling of the log/list
            refresh_interval=1.0,  # keep the title-bar clock ticking
        )
        # Make ESC feel instant. Two separate waits delay it by default:
        #  - ttimeoutlen (0.5s): a lone ESC might begin a terminal escape
        #    sequence (arrows, F-keys) — matters in explorer mode.
        #  - timeoutlen (1.0s): ESC is the prefix of the command line's Alt-key
        #    chords (Alt+b/f/d …), so it waits for a possible second key —
        #    this is the delay felt in shell mode.
        # Locally, multi-byte sequences / Alt-combos arrive in one read, so a
        # short timeout keeps them working. Raise these if keys misbehave over
        # slow/remote links.
        application.ttimeoutlen = 0.05
        application.timeoutlen = 0.05
        return application

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
        # the "nsh" label adopts the menu's header colour while a menu is open,
        # so it's obvious a popup is active; otherwise it blends into the bar.
        name_style = "class:menu.title" if self.menu.active else "class:titlebar.name"
        segs = [
            (name_style, " nsh "),
            ("class:titlebar", " "),
            ("class:titlebar.path", shorten_home(self.cwd)),
        ]
        if self.git_status.is_repo and self.git_status.branch:
            g = self.git_status
            # behind upstream -> yellow with the count; else dirty -> red; else green
            if g.behind > 0:
                style, text = "class:titlebar.branch.behind", f"⎇ {g.branch} ↓{g.behind}"
            elif g.dirty:
                style, text = "class:titlebar.branch.dirty", f"⎇ {g.branch}"
            else:
                style, text = "class:titlebar.branch", f"⎇ {g.branch}"
            segs.append(("class:titlebar", "  on "))
            segs.append((style, text))
        if self.mode == EXPLORER and self.explorer.selected:
            segs.append(("class:titlebar", "   "))
            segs.append(("class:titlebar.sel", f"● {len(self.explorer.selected)} selected"))
        clock = [("class:titlebar.clock", f" {datetime.now().strftime('%H:%M:%S')} ")]
        return self._fill_with_right(segs, "class:titlebar", clock)

    def _status_text(self):
        if self.mode == EXPLORER:
            hints = [
                ("↑↓", "move"), ("↵", "open"), ("Space", "select"),
                ("Tab", "actions"), ("b", "marks"), ("/", "find"),
                (":", "cmd"), ("q", "quit"),
            ]
        elif self.mode == SEARCH:
            hints = [
                ("type", "filter"), ("↑↓", "move"), ("↵", "select"),
                ("ESC", "cancel"),
            ]
        else:
            hints = [
                ("Tab", "complete"), ("↵", "run"), ("↑↓", "history"),
                ("PgUp/PgDn", "scroll"), ("^C", "stop"), ("^D", "quit"),
                ("ESC", "explorer"),
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
    def set_cwd(self, path, select_name=None):
        path = Path(path)
        try:
            os.chdir(path)
        except OSError as exc:
            self.set_message(f"cannot enter: {exc}")
            return
        self.cwd = path.resolve()
        self.explorer.selected.clear()
        self.explorer.load()
        # put the cursor on ``select_name`` (e.g. the directory we came up from)
        self.explorer.cursor = 0
        if select_name:
            for i, e in enumerate(self.explorer.entries):
                if e.name == select_name:
                    self.explorer.cursor = i
                    break
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

    # -- focus / overlays -----------------------------------------------------
    def _restore_focus(self):
        if self.mode == SHELL:
            self.application.layout.focus(self.shell.command_buffer)
        else:
            self.application.layout.focus(self.explorer.control)

    # -- confirmation dialog --------------------------------------------------
    def confirm(self, label, callback):
        """Show a centered yes/no dialog; ``callback(True|False)`` on resolve."""
        self.confirm_dialog.open("Confirm", label, callback)
        self.application.layout.focus(self.confirm_dialog.control)
        self.invalidate()

    # -- action menu ----------------------------------------------------------
    def open_menu(self, title, items):
        self.menu.open(title, items)
        self.application.layout.focus(self.menu.control)
        self.invalidate()

    def _menu_closed(self):
        self._restore_focus()
        self.invalidate()

    # -- input dialog ---------------------------------------------------------
    def open_input_dialog(self, title, text, cursor, on_accept):
        self.dialog.open(title, text, cursor, on_accept)
        self.application.layout.focus(self.dialog.control)
        self.invalidate()

    def _dialog_closed(self):
        self._restore_focus()
        self.invalidate()

    # -- bookmarks ------------------------------------------------------------
    def open_bookmark_menu(self):
        cwd = str(self.cwd)
        bookmarked = self.bookmarks.contains(cwd)
        items = [
            ("✓ Remove this directory" if bookmarked else "★ Bookmark this directory",
             self._toggle_bookmark),
        ]
        for p in self.bookmarks.list():
            items.append((f"  {shorten_home(p)}", lambda p=p: self.set_cwd(p)))
        self.open_menu("Bookmarks", items)

    def _toggle_bookmark(self):
        added = self.bookmarks.toggle(str(self.cwd))
        self.set_message("bookmarked" if added else "bookmark removed")

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
        # if a shell command is still running, confirm before quitting
        if self.runner.is_running():
            self.confirm("A command is still running. Quit anyway?", self._confirm_quit)
            return
        self._do_exit()

    def _confirm_quit(self, ok):
        if ok:
            self.runner.interrupt()  # don't leave the command orphaned
            self._do_exit()

    def _do_exit(self):
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
