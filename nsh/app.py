"""The nsh application: layout, the two modes, and central dispatch."""
import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition, has_completions
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
from .explorer.gitview import GitView
from .explorer.logview import LogView
from .explorer.preview import PreviewView
from .explorer.view import ExplorerView
from .search.view import SearchView
from .shell.runner import CommandRunner
from .shell.tabs import ShellTabs
from .util.bookmarks import Bookmarks
from .util.dialog import ConfirmDialog, InputDialog
from .util.menu import Menu
from .util.paths import shorten_home
from .util.width import text_width

EXPLORER = "explorer"
SHELL = "shell"
SEARCH = "search"
GIT = "git"
LOG = "gitlog"

# Once the shell output would shrink the explorer below this many rows, the
# shell takes over the whole screen.
SHELL_MIN_EXPLORER = 5


def _logical_path(path, base):
    """Absolute, lexically-normalised ``cd -L`` path: symlinks are NOT resolved.

    Joining ``..`` lexically (``normpath``) instead of letting the kernel walk
    the real directory tree is what keeps logical paths stable — entering a
    symlinked dir shows the path you followed, and going up returns to the
    directory that holds the link rather than the link target's real parent.
    """
    p = Path(path)
    if not p.is_absolute():
        p = Path(base) / p
    return Path(os.path.normpath(str(p)))


def _initial_logical_cwd():
    """Logical cwd at launch: honour ``$PWD`` (the shell's logical path) when it
    names the same directory as the physical cwd, else fall back to it."""
    here = os.getcwd()
    pwd = os.environ.get("PWD")
    if pwd and os.path.isabs(pwd):
        try:
            if os.path.samefile(pwd, here):
                return Path(os.path.normpath(pwd))
        except OSError:
            pass
    return Path(here)


class NshApp:
    def __init__(self, start_mode=None, query="", picker=False):
        self.cwd = _initial_logical_cwd()
        self.mode = EXPLORER
        # the mode to return to when leaving the shell (so a shell opened from
        # git mode goes back to git mode on ESC, not the explorer)
        self._shell_return = EXPLORER
        self.git_status = git.GitStatus()
        self._git_task = None

        # user configuration (~/.config/nsh/nshrc): colours + explorer keys
        config.ensure_default_config()
        color_overrides, key_overrides, settings, cfg_warning = config.load_user_config()
        self.keys = {**config.DEFAULT_KEYS, **key_overrides}
        self.style = config.build_style(color_overrides)
        self.settings = settings
        self.message = cfg_warning or ""
        self._msg_trim = 0    # chars trimmed off the front during the slide-out
        self._msg_gen = 0     # bumped per message, so a stale fade task bails

        # search-mode startup / result plumbing
        self._start_mode = start_mode
        self._initial_query = query
        self._pending_query = ""
        self.picker = picker
        self.search_result = None

        # app-level runner: only drives run_in_term (editors, etc.), no session
        self.runner = CommandRunner(self)
        # shell variables set with a bare `a=10` line; shared by every tab and
        # passed to each child command's environment (see CommandRunner).
        self.shell_vars = {}
        self.explorer = ExplorerView(self)
        self.shells = ShellTabs(self)  # the shell sessions, managed as tabs
        self.preview = PreviewView(self)
        self.show_preview = True
        self.search = SearchView(self)
        self.gitview = GitView(self)
        self.logview = LogView(self)
        self._log_return = EXPLORER  # the mode "Git: Log" was opened from

        # popup action menu (Tab in the explorer)
        self.menu = Menu(self._menu_closed)
        self.bookmarks = Bookmarks()
        self.visited = []  # most-recent-first history of directories we've left
        # last directory visited on each Windows drive letter (for "D:" changes)
        self._drive_dirs = {}
        self._remember_drive(self.cwd)
        # centered modal dialogs: text input (rename/new) and yes/no confirm
        self.dialog = InputDialog(self._dialog_closed)
        self.confirm_dialog = ConfirmDialog(self._dialog_closed)

        self.application = self._build_application()

    @property
    def shell(self):
        """The active shell session (most code only ever touches this one)."""
        return self.shells.current()

    def focus_shell(self):
        self.application.layout.focus(self.shells.current().command_buffer)

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
                    self.switch_mode(self._shell_return)
            elif self.mode == GIT:
                # clear the selection first, then leave git mode
                if self.gitview.selected:
                    self.gitview.clear_selection()
                else:
                    self.switch_mode(EXPLORER)
            elif self.mode == LOG:
                self.close_log()
            else:  # EXPLORER: clear any multi-selection
                self.explorer.clear_selection()

        @kb.add("c-g", filter=~overlay_open)
        def _(event):
            self.toggle_git_mode()

        @kb.add("c-q")
        def _(event):
            self.exit()

        @kb.add("c-c")
        def _(event):
            # Ctrl-C stops the active session's command (and its children) but
            # never quits nsh; with nothing running it just clears the input.
            if self.shell.runner.interrupt():
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

        # Alt+Up / Alt+Down scroll the output a line at a time. Both Win32 input
        # and VT100 terminals deliver Alt+<key> as an Escape prefix followed by
        # the key, so this is bound as the two-key sequence escape+arrow. (Plain
        # Up/Down stay free for command history.)
        @kb.add("escape", "up", filter=shell_mode)
        def _(event):
            self.shell.scroll(-1)

        @kb.add("escape", "down", filter=shell_mode)
        def _(event):
            self.shell.scroll(1)

        # shell tabs: switch with Alt+Left/Right, new with Ctrl+T, close Ctrl+W.
        # (Alt+arrow arrives as Escape+arrow on both Win32 and VT100; Ctrl+PgUp/
        # PgDn is unusable — Windows Terminal forwards it without the Ctrl flag,
        # making it indistinguishable from a plain PageUp scroll.)
        @kb.add("escape", "left", filter=shell_mode)
        @kb.add("f7", filter=shell_mode)
        def _(event):
            self.shells.prev()

        @kb.add("escape", "right", filter=shell_mode)
        @kb.add("f8", filter=shell_mode)
        def _(event):
            self.shells.next()

        @kb.add("c-t", filter=shell_mode)
        def _(event):
            self.shells.new_session()
            self.invalidate()

        @kb.add("f2", filter=shell_mode)
        def _(event):
            self.shells.rename()

        @kb.add("c-w", filter=shell_mode)
        def _(event):
            self.close_shell_tab()

        # Tab completion: Tab opens the menu (first item selected); while it is
        # open the arrows or j/k move. Tab and Space both accept; a directory is
        # reopened (no space) so it can be drilled into, anything else ends with
        # a trailing space.
        completing = shell_mode & has_completions

        @kb.add("tab", filter=shell_mode & ~has_completions)
        def _(event):
            event.current_buffer.start_completion(select_first=True)

        @kb.add("tab", filter=completing)
        def _(event):
            buff = event.current_buffer
            state = buff.complete_state
            comp = state.current_completion if state else None
            buff.complete_state = None  # accept the highlighted item
            if comp is None:
                return
            # a directory (its completion ends in a separator): reopen the menu so
            # its contents can be drilled into, with no trailing space; anything
            # else ends with a space, like Space.
            if comp.text.endswith(("/", "\\")):
                buff.start_completion(select_first=True)
            else:
                buff.insert_text(" ")

        @kb.add("space", filter=completing)
        def _(event):
            buff = event.current_buffer
            buff.complete_state = None  # accept the highlighted item…
            buff.insert_text(" ")       # …and end it with a space

        @kb.add("up", filter=completing)
        @kb.add("k", filter=completing)
        def _(event):
            event.current_buffer.complete_previous()

        @kb.add("down", filter=completing)
        @kb.add("j", filter=completing)
        def _(event):
            event.current_buffer.complete_next()

        # Ctrl-D on an empty command line closes the current tab (shell
        # convention); with text present the filter is false, so the default
        # delete-char still applies. Closing the last tab leaves shell mode.
        shell_line_empty = shell_mode & Condition(
            lambda: not self.shell.command_buffer.text
        )

        @kb.add("c-d", filter=shell_line_empty)
        def _(event):
            self.close_shell_tab()

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

        # git mode: the changed-file list beside the diff preview
        self._git_split = VSplit(
            [
                self.gitview.window,
                Window(width=1, char="│", style="class:preview.border"),
                self.preview.window,
            ]
        )
        git_area = DynamicContainer(
            lambda: self._git_split
            if (self.show_preview and self._wide_enough())
            else self.gitview.window
        )

        # git log mode: the graph/oneline history beside the commit preview
        self._log_split = VSplit(
            [
                self.logview.window,
                Window(width=1, char="│", style="class:preview.border"),
                self.preview.window,
            ]
        )
        log_area = DynamicContainer(
            lambda: self._log_split
            if (self.show_preview and self._wide_enough())
            else self.logview.window
        )

        # shell mode keeps the explorer on top, command line + output below
        self._shell_split = HSplit(
            [
                explorer_area,
                Window(height=1, char="─", style="class:preview.border"),
                self.shells.container,
            ]
        )

        def _body():
            if self.mode == SEARCH:
                return self.search.container
            if self.mode == GIT:
                return git_area
            if self.mode == LOG:
                return log_area
            if self.mode == SHELL:
                # grow with output, then take the whole screen at the cap
                return self.shells.container if self.shell_fullscreen() else self._shell_split
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
                # row 1 = directly under the title bar (row 0). left=0: the menu
                # rows carry a one-space left pad of their own, so this lands the
                # text in column 1 — flush under the "n" of the "nsh" label.
                Float(top=1, left=0, content=self.menu.container),
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
            # behind upstream -> yellow -count; uncommitted changes -> red;
            # committed-but-unpushed (ahead) -> yellow +count; else green
            if g.behind > 0:
                style, text = "class:titlebar.branch.behind", f"⎇ {g.branch} -{g.behind}"
            elif g.dirty:
                style, text = "class:titlebar.branch.dirty", f"⎇ {g.branch}"
            elif g.ahead > 0:
                style, text = "class:titlebar.branch.behind", f"⎇ {g.branch} +{g.ahead}"
            else:
                style, text = "class:titlebar.branch", f"⎇ {g.branch}"
            segs.append(("class:titlebar", " on "))
            segs.append((style, text))
            if g.in_progress:  # mid merge/rebase: flag it (resolve via git mode)
                segs.append(("class:titlebar", " "))
                segs.append(("class:titlebar.branch.dirty", f"⚠ {g.in_progress}"))
        if self.mode == GIT:
            segs.append(("class:titlebar", "   "))
            segs.append(("class:titlebar.branch", "● git"))
        if self.mode == LOG:
            segs.append(("class:titlebar", "   "))
            segs.append(("class:titlebar.branch", "● log"))
        selected = self.active_selection()
        if selected:
            segs.append(("class:titlebar", "   "))
            segs.append(("class:titlebar.sel", f"● {len(selected)} selected"))
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
        elif self.mode == GIT:
            hints = [
                ("↑↓", "move"), ("Space", "select"), ("Tab", "actions"),
                ("b", "marks"), (":", "cmd"), ("^G/ESC", "exit"), ("q", "quit"),
            ]
        elif self.mode == LOG:
            hints = [
                ("↑↓", "move"), ("↵", "actions"), ("/", "search"), ("n", "next"),
                ("ESC/q", "back"),
            ]
        else:
            hints = [
                ("Tab", "complete"), ("↵", "run"), ("↑↓", "history"),
                ("PgUp/PgDn", "scroll"), ("^T/^W", "tab"), ("Alt+←→/F7·8", "switch"),
                ("^C", "stop"), ("ESC", "explorer"),
            ]
        segs = []
        # the message sits in front of the shortcuts; while it slides out the
        # front is trimmed so the shortcuts ease in from the left to fill the gap
        if self.message:
            visible = self.message[self._msg_trim:]
            if visible:
                segs.append(("class:statusbar.msg", f" {visible} "))
        for key, label in hints:
            segs.append(("class:statusbar.key", f" {key} "))
            segs.append(("class:statusbar", f"{label} "))
        return self._fill(segs, "class:statusbar")

    # -- modes ----------------------------------------------------------------
    def toggle_mode(self):
        self.switch_mode(EXPLORER if self.mode == SHELL else SHELL)

    def switch_mode(self, mode):
        # remember where the shell was opened from, to return there on ESC
        if mode == SHELL and self.mode in (EXPLORER, GIT):
            self._shell_return = self.mode
        self.mode = mode
        if mode == SHELL:
            self.focus_shell()
        elif mode == SEARCH:
            self.search.start(self._pending_query)
            self._pending_query = ""
            self.application.layout.focus(self.search.query_buffer)
        elif mode == GIT:
            self.gitview.load()
            self.application.layout.focus(self.gitview.control)
            asyncio.ensure_future(self.refresh_git())  # pull fresh status on entry
        elif mode == LOG:
            self.logview.load()
            self.application.layout.focus(self.logview.control)
        else:
            self.application.layout.focus(self.explorer.control)
        self.invalidate()

    def toggle_git_mode(self):
        if self.mode == GIT:
            self.switch_mode(EXPLORER)
            return
        if not self.git_status.is_repo:
            self.set_message("not a git repository")
            return
        self.switch_mode(GIT)

    def open_log(self):
        if not self.git_status.is_repo:
            self.set_message("not a git repository")
            return
        self._log_return = self.mode if self.mode in (EXPLORER, GIT) else EXPLORER
        self.switch_mode(LOG)

    def close_log(self):
        self.switch_mode(self._log_return)

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

    def _term_cols(self):
        try:
            return max(1, get_app().output.get_size().columns)
        except Exception:
            return 80

    def shell_fullscreen(self):
        """True once the output no longer fits the shared (split) layout."""
        cap = self._shell_cap()
        # Count wrapped rows (a long line spans several), short-circuiting at cap.
        return self.shell.display_rows(self._term_cols(), limit=cap) > cap

    def shell_split_output_rows(self):
        """Output rows in split mode: grows with content up to the cap."""
        cap = self._shell_cap()
        return max(0, min(self.shell.display_rows(self._term_cols(), limit=cap), cap))

    # -- command line ---------------------------------------------------------
    def run_in_shell(self, session, cmd):
        """Run ``cmd`` typed in ``session``. If that session is still running a
        command, the new one opens in a fresh tab instead of interleaving."""
        if not cmd.strip():
            return
        if session.busy():
            session = self.shells.new_session()
        session.title = self._cmd_title(cmd)
        # echo the command first: it bakes the previous command's run-time badge
        # into the scrolled-up line, so reset only clears the live prompt below.
        session.append_command(cmd)
        session.runner.reset_result()  # clear the previous command's status tint
        if not self._handle_builtin(session, cmd):
            asyncio.ensure_future(self._exec(session, cmd))

    @staticmethod
    def _cmd_title(cmd):
        parts = cmd.split()
        return os.path.basename(parts[0]) if parts else "shell"

    def _handle_builtin(self, session, cmd):
        stripped = cmd.strip()
        if stripped in ("exit", "quit"):
            self.shells.close(session)  # close this tab (or leave shell mode)
            return True
        if stripped in ("clear", "cls"):
            session.clear()
            return True
        if stripped == "cd" or stripped.startswith("cd "):
            target = stripped[2:].strip() or "~"
            # logical (cd -L) target; set_cwd normalises and keeps symlinks
            path = _logical_path(os.path.expanduser(target), self.cwd)
            if path.is_dir():
                self.set_cwd(path)
            else:
                session.append(f"cd: no such directory: {target}", "class:shell.error")
            return True
        # Windows drive change: a bare "D:", or a drive-letter path ("D:\\dir"),
        # typed on its own. We cd to it when it resolves to a directory — a bare
        # "D:" goes to the last place we were on that drive, else its root. A file
        # path like "D:\\tool.exe" isn't a directory, so it falls through to run.
        if os.name == "nt" and self._is_drive_path(stripped):
            target = self._resolve_drive_path(stripped)
            if target.is_dir():
                self.set_cwd(target)
                return True
        # a bare `a=10` line: evaluate it once and store it in nsh's own env so
        # every later subprocess inherits it (each command runs in its own one).
        if session.runner.assignment_names(cmd):
            asyncio.ensure_future(session.runner.eval_assignment(cmd))
            return True
        return False

    async def _exec(self, session, cmd):
        runner = session.runner
        try:
            if runner.is_git_network(cmd):
                await self._exec_git_network(session, runner, cmd)
            elif runner.is_interactive(cmd):
                await runner.run_in_term(cmd)
            else:
                await runner.run(cmd)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            session.append(f"nsh: {exc}", "class:shell.error")
        self.invalidate()

    async def _exec_git_network(self, session, runner, cmd):
        """Run a remote git command (push/pull/fetch/clone) optimistically.

        With credential prompts disabled it streams through the pipe like any
        other command — so its output lands in the scrollback — whenever the
        credentials are already cached. If git can't get them it fails fast; we
        then retry on a real terminal, where it can prompt for a username /
        password, and leave a one-line note of how it ended (its terminal output
        isn't captured in the scrollback).
        """
        await runner.run(cmd, allow_prompt=False)
        if runner.auth_prompt_needed():
            session.append("git: credentials required — retrying on the terminal…",
                           "class:preview.dim")
            rc = await runner.run_in_term(cmd)
            runner.adopt_term_result(rc)  # prompt badge reflects the real outcome
            session.append(*runner.git_summary(cmd, rc))

    def close_shell_tab(self):
        """Ctrl-W: close the active tab, confirming first if it's still busy."""
        session = self.shells.current()
        if session.busy():
            self.confirm("A command is still running. Close this tab?",
                         lambda ok: self.shells.close(session) if ok else None)
        else:
            self.shells.close(session)

    # -- Windows drive changes ("D:") -----------------------------------------
    @staticmethod
    def _is_drive_path(line):
        """A bare ``X:`` or ``X:\\path`` typed on its own (no command/args)."""
        return (len(line) >= 2 and line[0].isalpha() and line[1] == ":"
                and " " not in line and "\t" not in line)

    def _resolve_drive_path(self, line):
        """Resolve a drive path to a target directory. Bare ``X:`` -> the last
        directory we were on that drive (else its root); ``X:\\p`` -> that path;
        ``X:p`` -> ``p`` relative to the remembered directory."""
        drive, rest = line[0].upper(), line[2:]
        root = self._drive_dirs.get(drive, Path(f"{drive}:\\"))
        if not rest:
            return root
        if rest[0] in "\\/":
            return Path(f"{drive}:{rest}")
        return root / rest

    def _remember_drive(self, path):
        """Record ``path`` as the latest location on its drive, so a later
        ``X:`` returns here (like cmd's per-drive current directory)."""
        if os.name == "nt" and path.drive:
            self._drive_dirs[path.drive[0].upper()] = path

    def edit_file(self, path):
        """Open ``path`` in a text editor.

        Precedence: the nshrc ``[general] editor`` setting, then $EDITOR /
        $VISUAL, then a platform default (notepad on Windows, vi elsewhere).
        """
        editor = (self.settings.get("editor") or os.environ.get("EDITOR")
                  or os.environ.get("VISUAL")
                  or ("notepad" if os.name == "nt" else "vi"))
        asyncio.ensure_future(self.runner.run_in_term(f'{editor} "{path}"'))

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
        # Keep the logical (cd -L) path: don't resolve symlinks. Crucially we
        # chdir to the lexically-normalised target, not the raw path — chdir-ing
        # through "<symlink>/.." would let the kernel walk into the link target
        # and land us in the wrong directory.
        target = _logical_path(path, self.cwd)
        try:
            os.chdir(target)
        except OSError as exc:
            self.set_message(f"cannot enter: {exc}")
            return
        old, new = str(self.cwd), str(target)
        if old != new:
            # keep a most-recent-first history of the directories we leave (for
            # the "recent directories" menu); drop the one we're entering, cap it
            self.visited = [d for d in self.visited if d not in (old, new)]
            self.visited.insert(0, old)
            del self.visited[50:]
        self.cwd = target
        self._remember_drive(target)  # so a later "X:" returns to here
        self.explorer.selected.clear()
        self.explorer.expanded.clear()  # the tree is relative to the old cwd
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
        # a directory change drops any pending "return to git mode" (e.g. a cd
        # run from a shell opened in git mode) — git mode was for the old dir
        self._shell_return = EXPLORER
        # changing directory is meaningless in git mode (it's repo-wide); leave it
        if self.mode == GIT:
            self.switch_mode(EXPLORER)
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
            self.gitview.on_status_changed()
            self.invalidate()

    async def refresh_git(self):
        self.git_status = await git.query(self.cwd)
        self.gitview.on_status_changed()
        self.invalidate()

    # -- focus / overlays -----------------------------------------------------
    def _restore_focus(self):
        if self.mode == SHELL:
            self.focus_shell()
        elif self.mode == GIT:
            self.application.layout.focus(self.gitview.control)
        elif self.mode == LOG:
            self.application.layout.focus(self.logview.control)
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

    # -- navigation shortcuts -------------------------------------------------
    def go_home(self):
        self.set_cwd(str(Path.home()))

    def open_visited_menu(self):
        """A menu of recently visited directories (jump back to one)."""
        if not self.visited:
            self.set_message("no visited directories yet")
            return
        items = [(f"  {shorten_home(p)}", lambda p=p: self.set_cwd(p))
                 for p in self.visited]
        self.open_menu("Recent directories", items)

    def active_selection(self):
        """The marked-paths set for the current mode (explorer / git), else None."""
        if self.mode == EXPLORER:
            return self.explorer.selected
        if self.mode == GIT:
            return self.gitview.selected
        return None

    # -- misc -----------------------------------------------------------------
    def set_message(self, message):
        self.message = message
        self._msg_trim = 0
        self._msg_gen += 1
        self.invalidate()
        if message:
            # show it for 5s, then slide the hints in from the left over it
            try:
                asyncio.ensure_future(self._fade_message(self._msg_gen))
            except RuntimeError:
                pass  # no running loop yet (e.g. a startup warning)

    async def _fade_message(self, gen):
        """After 5s, animate the status message off to the left (the shortcuts
        slide in to fill the space), then clear it. Bails if a newer message
        superseded this one."""
        try:
            await asyncio.sleep(5.0)
            n = len(self.message)
            frames = 15
            for f in range(1, frames + 1):
                if gen != self._msg_gen:
                    return
                self._msg_trim = round(n * f / frames)
                self.invalidate()
                await asyncio.sleep(0.04)
        except asyncio.CancelledError:
            raise
        if gen == self._msg_gen:
            self.message = ""
            self._msg_trim = 0
            self.invalidate()

    def invalidate(self):
        try:
            self.application.invalidate()
        except Exception:
            pass

    def exit(self):
        # if any shell session is still running, confirm before quitting
        if self.shells.any_running():
            self.confirm("A command is still running. Quit anyway?", self._confirm_quit)
            return
        self._do_exit()

    def _confirm_quit(self, ok):
        if ok:
            self.shells.interrupt_all()  # don't leave commands orphaned
            self._do_exit()

    def _do_exit(self):
        try:
            self.application.exit()
        except Exception:
            pass

    async def _watch_cwd(self):
        """Poll the current directory and auto-refresh when it changes."""
        while True:
            try:
                await asyncio.sleep(1.0)
                if self.mode == GIT:
                    await self.refresh_git()  # reflect external edits in the list/diff
                else:
                    self.explorer.check_external_change()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let the watcher die
                pass

    async def run_async(self):
        self.explorer.load()
        self.schedule_git()
        if self._start_mode == SHELL:
            self.switch_mode(SHELL)
        elif self._start_mode == SEARCH:
            self.enter_search(self._initial_query)
        watcher = asyncio.ensure_future(self._watch_cwd())
        try:
            await self.application.run_async()
        finally:
            watcher.cancel()
        return self.search_result
