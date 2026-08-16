"""The nsh application: layout, the two modes, and central dispatch."""
import asyncio
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.key_binding import (
    DynamicKeyBindings, KeyBindings, merge_key_bindings)
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.mouse_events import MouseEventType

from . import __version__, config
from .explorer import git
from .explorer.browse import BranchBrowser
from .explorer.preview import PreviewView
from .notes.view import NotesView
from .network import backend as remote
from .network.shell import RemoteShellView
from .network.view import NetworkView
from .preferences.view import PreferencesView
from .search.view import SearchView
from .system.view import SystemView
from .shell.quoting import quote_arg, unquote_body
from .shell.runner import CommandRunner
from .shell.tabs import ShellTabs
from .util import state
from .util.bookmarks import Bookmarks
from .util.dialog import (
    ChmodDialog, ConfirmDialog, FindTextDialog, InfoDialog, InputDialog,
    ProgressDialog)
from .util.menu import SEPARATOR, Menu
from .util.paths import shorten_home
from .util.widgets import WheelScrollControl
from .util.width import char_width, cut_to_width, text_width

EXPLORER = "explorer"
SHELL = "shell"
SEARCH = "search"
GIT = "git"
LOG = "gitlog"
NOTES = "notes"
SYSTEM = "system"
NETWORK = "network"
REMOTE_SHELL = "remote-shell"
PREFERENCES = "preferences"

# Once the shell output would shrink the explorer below this many rows, the
# shell takes over the whole screen.
SHELL_MIN_EXPLORER = 5

# Status messages may include text supplied by external programs (notably SSH
# and ProxyCommand failures). Never let terminal control sequences or line
# breaks escape into prompt_toolkit's one-row status window.
_TERMINAL_ESCAPE_RE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|[P^_].*?\x1b\\|[@-_][0-?]*[ -/]*[@-~])"
)


def _safe_status_message(message):
    text = _TERMINAL_ESCAPE_RE.sub("", str(message))
    text = "".join(
        " " if ord(char) < 32 or 127 <= ord(char) < 160 else char
        for char in text
    )
    return " ".join(text.split())
PANE_SEPARATOR = "║"


def _unquote_arg(s):
    """Strip a surrounding quote pair from a single shell argument.

    Tab-completion wraps a name with a metacharacter in double quotes (a
    directory keeps its quote open, e.g. ``"New folder/``); ``cd`` gets the raw
    line, so peel the leading quote and a matching trailing one before treating
    it as a path. A double-quoted POSIX name may carry backslash escapes (``\\$``
    etc.) — undo those too, since ``cd`` resolves the path itself without a shell.
    """
    s = s.strip()
    if s[:1] in ("'", '"'):
        q = s[0]
        s = s[1:]
        if s.endswith(q):
            s = s[:-1]
        if q == '"':
            s = unquote_body(s)
    return s


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
        initial_cwd = _initial_logical_cwd()
        # self.mode is a per-tab property (see below) backed by the active tab's
        # session; the first tab is created as EXPLORER inside ShellTabs, so no
        # eager assignment is needed (and the property can't be set before then)
        # the mode to return to when leaving the shell (so a shell opened from
        # git mode goes back to git mode on ESC, not the explorer)
        self._shell_return = EXPLORER
        # git status lives per explorer pane (see the git_status property); each
        # pane can be in a different repo so both panes' markers stay correct
        self._git_task = None

        # user configuration (~/.config/nsh/nshrc): colours + explorer keys
        config.ensure_default_config()
        color_overrides, key_overrides, settings, cfg_warning = config.load_user_config()
        self.keys = {**config.DEFAULT_KEYS, **key_overrides}
        self.style = config.build_style(color_overrides)
        self.settings = settings
        self.message = cfg_warning or ""
        # watch nshrc so edits (e.g. remapped keys) apply without a restart
        self._config_mtime = self._read_config_mtime()
        self._config_check_at = 0.0  # throttle the stat to ~once a second

        # search-mode startup / result plumbing
        self._start_mode = start_mode
        self._initial_query = query
        self._pending_query = ""
        self._search_remote = False
        self._search_return = EXPLORER
        self.picker = picker
        self.search_result = None

        # app-level runner: only drives run_in_term (editors, etc.), no session
        self.runner = CommandRunner(self)
        # shell variables set with a bare `a=10` line; shared by every tab and
        # passed to each child command's environment (see CommandRunner).
        self.shell_vars = {}
        # copy / cut buffer for the explorer, ([Path, ...], "copy" | "cut").
        # Shared by every tab so you can copy in one tab and paste in another.
        self.clipboard = None
        # files the user `source`d: each command runs in its own subprocess, so a
        # sourced script's functions/aliases/vars would vanish — instead we re-
        # source these (silently) ahead of every later command (see CommandRunner).
        self.sourced_files = []
        # Tabs: each tab owns its own explorer pane(s) AND its own shell session
        # (see ShellTabs). Opening a tab opens a fresh explorer; switching tabs
        # swaps the whole working context. The explorers / active_pane / two_pane
        # properties below always point at the current tab's state.
        #
        # Within a tab there are two side-by-side explorer panes (Norton/MC-
        # style): single-pane mode shows only the active one; two-pane mode shows
        # both. Each pane keeps its own directory, selection and cursor, and each
        # tab keeps its own two-pane flag (so one tab can be split while another
        # isn't); new tabs start from the nshrc default below.
        self._two_pane_default = (
            self.settings.get("two_pane", "false").strip().lower()
            in ("true", "1", "yes", "on"))
        self._restore_tabs = (
            self.settings.get("restore_tabs", "true").strip().lower()
            in ("true", "1", "yes", "on"))
        saved_tabs = (state.get("explorer_tabs")
                      if self._restore_tabs and not picker else None)
        self.shells = ShellTabs(self, initial_cwd, saved_tabs)
        try:
            os.chdir(self.explorer.cwd)
        except OSError:
            pass
        # zoom: when on, the split gives the focused pane a 9:1 share instead of
        # an even 5:5 (the big pane follows the focus). See toggle_zoom / _pane_dim.
        # (zoom stays app-wide, applied to whichever tab is current.)
        self.zoom = False
        # last (tag, index, time) left-click, for detecting a double-click that
        # opens the row a single click only moved the cursor to (see double_click)
        self._last_click = None
        self.preview = PreviewView(self)
        self.show_preview = True
        self.search = SearchView(self)
        # logview is per-tab (see the logview property); each tab is created with
        # its own in ShellTabs, so no app-wide instance is built here
        self.notesview = NotesView(self)
        self.systemview = SystemView(self)
        self.networkview = NetworkView(self)
        self.remote_shell = RemoteShellView(self)
        self.preferencesview = PreferencesView(self)
        self._notes_return = EXPLORER  # the mode Notes was opened from
        self._notes_return_pane = 1
        self._preferences_return = EXPLORER
        self._preferences_return_pane = 1
        self._system_return = EXPLORER
        self._system_return_pane = 1
        self._shell_return_pane = 1
        self._find_return = EXPLORER
        self._find_return_pane = 1
        self._git_return = EXPLORER
        self._git_return_pane = 1
        self._log_return_pane = 1

        # popup action menu (Tab in the explorer)
        self.menu = Menu(self._menu_closed)
        # whether the open menu is anchored at the cursor row (vs. the top under
        # the "nsh" label); only the latter tints the "nsh" label (see _title_text)
        self._menu_at_cursor = False
        self._menu_return_focus = None
        self._dialog_return_focus = None
        self.bookmarks = Bookmarks()
        self.visited = []  # most-recent-first history of directories we've left
        # last directory visited on each Windows drive letter (for "D:" changes)
        self._drive_dirs = {}
        self._remember_drive(self.cwd)
        # centered modal dialogs: text input (rename/new), yes/no confirm, and
        # the read-only About box
        self.dialog = InputDialog(self._dialog_closed)
        self.confirm_dialog = ConfirmDialog(self._dialog_closed)
        self.about_dialog = InfoDialog(self._dialog_closed)
        self.find_dialog = FindTextDialog(self._dialog_closed)
        self.chmod_dialog = ChmodDialog(self._dialog_closed)
        self.progress_dialog = ProgressDialog(self._dialog_closed)
        # browse a git branch's file tree (from the branch action menu)
        self.branch_browser = BranchBrowser(self)

        self.application = self._build_application()

    @property
    def explorers(self):
        """The current tab's pair of explorer panes (each tab has its own)."""
        return self.shells.current().explorers

    @property
    def active_pane(self):
        """Index (0/1) of the current tab's active explorer pane."""
        return self.shells.current().active_pane

    @active_pane.setter
    def active_pane(self, value):
        self.shells.current().active_pane = value

    @property
    def two_pane(self):
        """Whether the current tab shows both explorer panes (per-tab state)."""
        return self.shells.current().two_pane

    @two_pane.setter
    def two_pane(self, value):
        self.shells.current().two_pane = value

    def _all_explorers(self):
        """Every explorer pane across all tabs (for config-reload key rebuilds)."""
        return [ex for s in self.shells.sessions for ex in s.explorers]

    @property
    def explorer(self):
        """The active explorer pane — the one holding the cursor. Most code only
        ever touches this one; the cwd, git status and shell follow it."""
        return self.explorers[self.active_pane]

    @property
    def cwd(self):
        """The active pane's directory (the process cwd is kept chdir'd to it)."""
        return self.explorers[self.active_pane].cwd

    @property
    def git_status(self):
        """The active pane's git status (title bar, prompt, git mode follow it)."""
        return self.explorers[self.active_pane].git_status

    @property
    def gitview(self):
        """The current tab's git-mode view — each tab keeps its own changed-file
        list, cursor and selection, so F7/F8 swap it along with the explorer."""
        return self.shells.current().gitview

    @property
    def mode(self):
        """The current tab's mode (explorer / shell / git / …). It's per-tab, so
        leaving git mode in one tab doesn't pull the others out of it; switching
        tabs adopts the now-active tab's mode."""
        return self.shells.current().mode

    @mode.setter
    def mode(self, value):
        self.shells.current().mode = value

    def _all_gitviews(self):
        """Every tab's git view (for config-reload key rebuilds)."""
        return [s.gitview for s in self.shells.sessions]

    @property
    def logview(self):
        """The current tab's git-log view — each tab keeps its own history list,
        cursor and search, so F7/F8 swap it along with the explorer."""
        return self.shells.current().logview

    def _all_logviews(self):
        """Every tab's log view (for config-reload key rebuilds)."""
        return [s.logview for s in self.shells.sessions]

    @property
    def _log_return(self):
        """The mode the log was opened from, per tab (since the log is per-tab)."""
        return self.shells.current().log_return

    @_log_return.setter
    def _log_return(self, value):
        self.shells.current().log_return = value

    @property
    def shell(self):
        """The active shell session (most code only ever touches this one)."""
        return self.shells.current()

    def focus_shell(self):
        self.application.layout.focus(self.shells.current().command_buffer)

    def _sync_network_local_pane(self):
        """Point the local half of the connected local|remote layout at the
        current tab's active explorer, so each tab browses (and transfers to and
        from) its *own* local directory beside the one shared remote pane."""
        if self.networkview.connected:
            self.networkview.local_view = self.explorer

    def remember_network_pane(self):
        """Record which half of the local|remote split the outgoing tab was on.

        Network mode is per-tab like every other mode, but the local|remote
        sub-focus inside it was not stored anywhere, so coming back to a tab
        alwyas landed on the shared remote pane. Called just before a tab stops
        being current (switch / new tab), mirroring what the shell, git, log and
        notes round trips already do with _network_pane_direction().
        """
        if self.mode == NETWORK:
            self.shells.current().network_pane = self._network_pane_direction()

    def _after_tab_switch(self):
        """Apply the consequences of a different tab becoming current: the
        process cwd, preview, git status and focus all follow the now-active
        tab's explorer. Used by every tab switch (keys / clicks) and tab close,
        staying in the current mode rather than forcing shell mode."""
        # the connection owns the right pane in every tab, so a plain explorer
        # tab (a freshly opened one included) adopts the local|remote layout
        # instead of growing a third pane beside the preview. It starts on its
        # own local list - the remote pane is a Shift+L away.
        promoted = self.networkview.connected and self.mode == EXPLORER
        if promoted:
            self.mode = NETWORK
        self._sync_network_local_pane()
        ex = self.explorer
        try:
            os.chdir(ex.cwd)  # the process cwd follows the active tab's pane
        except OSError:
            pass
        self._remember_drive(ex.cwd)
        # The directory may have changed while this tab was away. Do not block
        # the tab switch on a large directory scan.
        asyncio.ensure_future(ex.check_external_change())
        self.preview.clear()
        self.message = ""
        self.schedule_git()
        # in git / log mode, refresh the now-active tab's own list (each keeps
        # its own cursor, which the reload preserves)
        if self.mode == GIT:
            self.gitview.load()
        elif self.mode == LOG:
            self.logview.load()
        self._restore_focus()
        self.invalidate()

    def shell_insert_paths(self, paths):
        """Open the shell and insert ``paths`` (quoted, relative to the cwd) at
        the command-line cursor — used by the explorer's "send to shell" key."""
        if not paths:
            return
        is_posix = self.shells.current().runner._is_posix
        parts = [quote_arg(os.path.relpath(str(p), str(self.cwd)).replace(os.sep, "/"),
                           is_posix) for p in paths]
        text = " ".join(parts)
        # splice the names in at the cursor, space-separated from any existing text
        self.switch_mode(SHELL)
        buff = self.shells.current().command_buffer
        before = buff.document.text_before_cursor
        if before and not before.endswith(" "):
            buff.insert_text(" ")  # keep names separate from what's already typed
        buff.insert_text(text + " ")
        # park the cursor at the very front so the command name can be typed
        # ahead of the file(s) that were just spliced in
        buff.cursor_position = 0

    def _build_pane_keys(self):
        """The remappable pane_prev / pane_next pair (F7/F8 by default), in their
        own KeyBindings so reload_config() can swap them live. They move between
        tabs only — in the explorer, the shell and git mode alike (each tab owns
        its own explorer, shell and git view). They deliberately do
        *not* toggle list <-> preview focus (use Esc or a mouse click) or switch
        the two-pane active pane (click a pane). Ctrl combos are allowed; a bad
        key spec in nshrc is skipped, as elsewhere."""
        kb = KeyBindings()

        network_mode = Condition(lambda: self.mode == NETWORK)
        network_local = Condition(self.network_local_focused)

        # In the mixed local/remote view, copy from the focused local pane to
        # the remote pane. This eager binding overrides the local explorer's
        # ordinary copy action only while Network mode is visible; ``p`` keeps
        # its normal local paste meaning.
        @kb.add("c", filter=network_mode & network_local, eager=True)
        def _(event):
            self.networkview.upload()

        @kb.add("escape", filter=network_mode & network_local, eager=True)
        def _(event):
            (self.networkview.local_view or self.explorer).clear_selection()

        # The local explorer normally owns ``2`` (toggle explorer split).  The
        # network layout is already a fixed local/remote split, so swallowing it
        # avoids silently changing the hidden explorer layout underneath it.
        @kb.add("2", filter=network_mode & network_local, eager=True)
        def _(event):
            pass
        tab_mode = Condition(
            lambda: self.mode in (EXPLORER, SHELL, GIT, LOG, NETWORK, REMOTE_SHELL)
            and not self._overlay_active())

        def add(key, filt, handler, eager=False):
            if not key:
                return
            try:
                kb.add(key, filter=filt, eager=eager)(lambda event: handler())
            except Exception:  # noqa: BLE001 - bad key spec; skip it
                pass

        prev_key = self.keys.get("pane_prev")
        next_key = self.keys.get("pane_next")
        add(prev_key, tab_mode, self.shells.prev)
        add(next_key, tab_mode, self.shells.next)

        # Ctrl+Z normally suspends a terminal application on POSIX. Inside a
        # command shell it instead removes only the newest waiting queue item.
        queue_mode = Condition(
            lambda: self.mode in (SHELL, REMOTE_SHELL)
            and not self._overlay_active())
        add(self.keys.get("queue_remove_last"), queue_mode,
            self.remove_last_queued, eager=True)

        # zoom (z by default): enlarge the focused pane wherever a split is on
        # screen — the explorer (single + two-pane), git and log views. Guarded
        # against firing while a menu/dialog is up.
        zoom_mode = Condition(
            lambda: self.mode in (EXPLORER, GIT, LOG) and not self._overlay_active())
        add(self.keys.get("zoom"), zoom_mode, self.toggle_zoom)
        return kb

    def remove_last_queued(self):
        """Discard the most recently queued local/remote shell job."""
        shell = self.remote_shell if self.mode == REMOTE_SHELL else self.shell
        removed = shell.remove_last_pending()
        if removed is not None:
            self.set_message(f"removed from queue: {removed}")

    def shell_escape(self, remote=False):
        """Clear a typed command first; leave the local/SSH shell when empty."""
        shell = self.remote_shell if remote else self.shell
        if remote:
            self.switch_mode(NETWORK)
        else:
            self.leave_shell()

    # -- live config reload ---------------------------------------------------
    def _read_config_mtime(self):
        try:
            return config.config_path().stat().st_mtime
        except (OSError, RuntimeError):
            return None

    def _maybe_reload_config(self):
        """Reload nshrc when it changes on disk, so edits take effect without a
        restart. Called from the (per-second) title render; the stat is throttled
        so it doesn't run on every keypress."""
        now = time.monotonic()
        if now - self._config_check_at < 1.0:
            return
        self._config_check_at = now
        mtime = self._read_config_mtime()
        if mtime is not None and mtime != self._config_mtime:
            self._config_mtime = mtime
            self.reload_config()

    def reload_config(self):
        """Re-read nshrc and apply the new keys / colours without a restart.
        Every config-driven key binding is wrapped in DynamicKeyBindings, so
        rebuilding the underlying KeyBindings here makes the remaps take effect
        live; the style is read through a DynamicStyle, so reassigning it
        repaints with the new colours on the next frame."""
        color_overrides, key_overrides, settings, warning = config.load_user_config()
        self.keys = {**config.DEFAULT_KEYS, **key_overrides}
        self.settings = settings
        self._two_pane_default = (
            settings.get("two_pane", "false").strip().lower()
            in ("true", "1", "yes", "on"))
        self._restore_tabs = (
            settings.get("restore_tabs", "true").strip().lower()
            in ("true", "1", "yes", "on"))
        for session in self.shells.sessions:
            session.trim_scrollback()
        self.remote_shell.trim_scrollback()
        self.style = config.build_style(color_overrides)
        self.application.style = self.style
        self._pane_kb = self._build_pane_keys()
        sort = settings.get("sort", "name")
        reverse = (settings.get("sort_reverse", "false").strip().lower()
                   in ("true", "1", "yes", "on"))
        for ex in self._all_explorers():
            ex.sort, ex.reverse = sort, reverse
            ex.load()
            ex.rebuild_keys()
        if self.networkview.entries:
            self.networkview.set_sort(sort, reverse)
        for gv in self._all_gitviews():
            gv.rebuild_keys()
        for lv in self._all_logviews():
            lv.rebuild_keys()
        self.set_message(warning or "config reloaded")
        self.invalidate()

    # -- layout ---------------------------------------------------------------
    def _ensure_tab_layout(self, session):
        """Build (once, then cache) the explorer VSplits for ``session``'s own
        panes. Each tab has its own pair of explorer windows, so the split that
        places them beside the preview (or each other, in two-pane view) is built
        per tab; only the current tab's split is ever on screen. The width
        lambdas only fire for the visible tab, so indexing the active tab's panes
        (``_explorer_focused(0/1)``) stays correct."""
        if getattr(session, "_ex_split", None) is not None:
            return
        e0, e1 = session.explorers
        e0.window.width = lambda: self._pane_dim(self._explorer_focused(0))
        e1.window.width = lambda: self._pane_dim(self._explorer_focused(1))
        session._ex_split = VSplit([
            e0.window,
            Window(width=1, char="│", style="class:preview.border"),
            self.preview.window,
        ])
        session._two_split = VSplit([
            e0.window,
            Window(width=1, char=PANE_SEPARATOR, style="class:preview.border"),
            e1.window,
        ])

    def _network_local_container(self):
        """The local half of the connected local|remote split: the pane the
        transfers use, which is the current tab's (kept in sync on every tab
        pane switch). Going through _ensure_tab_layout first means a tab first
        shown while connected still gets its zoom-aware width."""
        self._ensure_tab_layout(self.shells.current())
        return (self.networkview.local_view or self.explorer).window

    def _explorer_area_container(self):
        """The current tab's explorer area: the lone local pane while a remote
        connection owns the right half, both panes in two-pane view, the single
        pane beside the preview when wide enough, else the bare listing."""
        session = self.shells.current()
        self._ensure_tab_layout(session)
        if self.networkview.connected:
            # the remote pane already fills the right half, so the local side
            # stays a single list - never a third pane beside it
            return session.explorers[session.active_pane].window
        if self.two_pane:
            return session._two_split
        if self.show_preview and self._wide_enough():
            return session._ex_split
        return session.explorers[0].window

    def _build_application(self):
        confirm_open = Condition(lambda: self.confirm_dialog.active)
        menu_open = Condition(lambda: self.menu.active)
        dialog_open = Condition(lambda: self.dialog.active)
        about_open = Condition(lambda: self.about_dialog.active)
        find_open = Condition(lambda: self.find_dialog.active)
        chmod_open = Condition(lambda: self.chmod_dialog.active)
        progress_open = Condition(lambda: self.progress_dialog.active)
        overlay_open = (confirm_open | menu_open | dialog_open | about_open
                        | find_open | chmod_open | progress_open)

        kb = KeyBindings()

        # F10 opens the nsh menu (Preferences / About) from any mode.
        @kb.add("f10", filter=~overlay_open)
        def _(event):
            self.open_nsh_menu()

        # Ctrl+F: Find — pick text (grep) or file (fuzzy). Explorer/git only.
        find_modes = Condition(lambda: self.mode in (EXPLORER, GIT))

        @kb.add("c-f", filter=~overlay_open & find_modes)
        def _(event):
            self.open_find()

        # Ctrl+N: Notes (a scratch pad of multi-line notes). Available from the
        # explorer, the git view, the shell, and the process manager.
        notes_modes = Condition(lambda: self.mode in (EXPLORER, GIT, SHELL, SYSTEM))

        @kb.add("c-n", filter=~overlay_open & notes_modes)
        def _(event):
            self.open_notes()

        @kb.add("escape", filter=~overlay_open)
        def _(event):
            self.message = ""  # ESC dismisses the status message
            # a zoomed pane backs out to the even split first (before clearing a
            # selection or leaving the mode); the preview pane handles its own
            # Esc, so this covers the list-focused case.
            if self._zoom_active():
                self.toggle_zoom()
                return
            if self.mode == SEARCH:
                self.cancel_search()
            elif self.mode == SHELL:
                self.shell_escape()
            elif self.mode == GIT:
                # clear the selection first, then leave git mode
                if self.gitview.selected:
                    self.gitview.clear_selection()
                else:
                    self.close_git()
            elif self.mode == LOG:
                self.close_log()
            elif self.mode == NOTES:
                self.leave_notes()
            elif self.mode == SYSTEM:
                self.close_system()
            elif self.mode == PREFERENCES:
                self.close_preferences()
            elif self.mode == REMOTE_SHELL:
                self.shell_escape(remote=True)
            else:  # EXPLORER: clear any multi-selection
                self.explorer.clear_selection()

        @kb.add("c-g", filter=~overlay_open)
        def _(event):
            self.toggle_git_mode()

        @kb.add("c-q", filter=~progress_open)
        def _(event):
            self.exit()

        @kb.add("c-c", filter=~progress_open)
        def _(event):
            # Ctrl-C stops the active session's command (and its children) but
            # never quits nsh; with nothing running it just clears the input.
            if self.mode == REMOTE_SHELL:
                if not self.remote_shell.interrupt():
                    self.remote_shell.command_buffer.reset()
                return
            if self.shell.runner.interrupt():
                self.shell.append("^C")
            elif self.mode == SHELL:
                self.shell.command_buffer.reset()

        paste_mode = Condition(
            lambda: self.mode in (SHELL, REMOTE_SHELL)
            and not self._overlay_active())

        @kb.add(Keys.BracketedPaste, filter=paste_mode, eager=True)
        def _(event):
            self.paste_shell_text(
                event.data, remote=self.mode == REMOTE_SHELL)

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
        def _(event):
            self.shells.prev()

        @kb.add("escape", "right", filter=shell_mode)
        def _(event):
            self.shells.next()

        # The same Alt+Left/Right switch tabs from the explorer too (each tab now
        # carries its own explorer), so a tab can be navigated to without a mouse.
        explorer_mode = Condition(lambda: self.mode == EXPLORER)
        # git and log modes are per-tab too, so Ctrl+T / Ctrl+W open / close tabs
        # and Alt+Left/Right switch them from there, just like the explorer
        git_mode = Condition(lambda: self.mode == GIT)
        log_mode = Condition(lambda: self.mode == LOG)
        network_mode = Condition(lambda: self.mode == NETWORK)
        remote_shell_mode = Condition(lambda: self.mode == REMOTE_SHELL)

        @kb.add("escape", "left", filter=explorer_mode | git_mode | log_mode)
        def _(event):
            self.shells.prev()

        @kb.add("escape", "right", filter=explorer_mode | git_mode | log_mode)
        def _(event):
            self.shells.next()

        # pane_prev / pane_next (F7/F8 by default) live in their own KeyBindings
        # (see _build_pane_keys), wrapped in DynamicKeyBindings below so a config
        # reload can rebuild just them and have the new keys take effect live.
        self._pane_kb = self._build_pane_keys()

        # Ctrl+T opens a new tab — a fresh explorer + shell — from the explorer,
        # the shell and git / log modes alike, staying in whichever mode you're in.
        @kb.add("c-t", filter=~overlay_open & (shell_mode | explorer_mode | git_mode | log_mode | network_mode | remote_shell_mode))
        def _(event):
            self.shells.new_session()

        @kb.add("f2", filter=shell_mode)
        def _(event):
            self.shells.rename()

        # Ctrl+W closes the current tab from the explorer, the shell and git / log
        # modes (closing the last tab just clears its shell and stays put).
        @kb.add("c-w", filter=~overlay_open & (shell_mode | explorer_mode | git_mode | log_mode | network_mode | remote_shell_mode))
        def _(event):
            self.close_shell_tab()

        # Tab completion: Tab opens the menu (first item selected); while it is
        # open the arrows or j/k move. Tab and Space both accept; a directory is
        # reopened (no space) so it can be drilled into, anything else ends with
        # a trailing space.
        completing = shell_mode & has_completions

        def _shell_complete(buff, first=True):
            """Tab with the menu closed: compute the candidates and act on the
            count — a single one is applied directly (no menu; a trailing space,
            or drilling into a unique directory), several open the menu.

            ``first`` is only false after auto-applying a unique directory: the
            menu of its contents then opens without pre-selecting an item, so the
            focus doesn't jump into it — the user picks or keeps typing.
            """
            if not buff.completer:
                return
            comps = list(buff.completer.get_completions(
                buff.document, CompleteEvent(completion_requested=True)))
            if len(comps) == 1:
                comp = comps[0]
                buff.apply_completion(comp)
                if comp.text.endswith(("/", "\\")):
                    _shell_complete(buff, first=False)  # drilled into a unique dir
                else:
                    buff.insert_text(" ")
            elif comps:
                buff.start_completion(select_first=first)

        @kb.add("tab", filter=shell_mode & ~has_completions)
        def _(event):
            _shell_complete(event.current_buffer)

        @kb.add("tab", filter=completing)
        def _(event):
            buff = event.current_buffer
            state = buff.complete_state
            comp = state.current_completion if state else None
            if comp is None:
                # the menu is open with nothing selected — the state left after
                # Tab auto-drilled into a unique directory (which deliberately
                # doesn't pre-select an item). A further Tab should step into the
                # menu, not close it, so completion can keep going.
                buff.complete_next()
                return
            buff.complete_state = None  # accept the highlighted item
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

        # Esc while the menu is open should just close the menu and revert the
        # highlighted items's text preview, not fall through to the global Esc
        # handler below (which clears the whole command line) - eager to it
        # wins over that ~overlay_open binding
        @kb.add("escape", filter=completing, eager=True)
        def _(event):
            event.current_buffer.cancel_completion()

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

        # zoom: give every split pane a focus-aware width weight, read live each
        # frame (so toggling zoom or moving focus reshapes the split). Off, the
        # weights are all 1 -> an even split; on, the focused pane wins 9:1.
        self.preview.window.width = lambda: self._pane_dim(self.preview_focused())
        # the git and log views' widths are set per tab (in ShellTabs._new_tab),
        # since each tab owns its own git / log windows

        # Each tab owns its own explorer pane(s), so the split that lays them out
        # (beside the preview, or each other in two-pane view) is built per tab
        # and chosen live — switching tabs swaps the listing, not just the shell.
        self._ensure_tab_layout(self.shells.current())

        # the explorer area, reused both in explorer mode and on top of the
        # shell so the listing stays visible. Two-pane shows both panes (no
        # preview); single-pane shows pane 0 with the optional preview beside it.
        explorer_area = DynamicContainer(self._explorer_area_container)

        # git mode: the changed-file list beside the diff preview. The list
        # window is per tab, so it's wrapped in a DynamicContainer that resolves
        # to the current tab's git view each frame.
        self._git_split = VSplit(
            [
                DynamicContainer(lambda: self.gitview.window),
                Window(width=1, char="│", style="class:preview.border"),
                self.preview.window,
            ]
        )
        git_area = DynamicContainer(
            lambda: self._git_split
            if (self.show_preview and self._wide_enough())
            else self.gitview.window
        )

        # git log mode: the graph/oneline history beside the commit preview. Like
        # the git list, the log window is per tab, so it's wrapped in a
        # DynamicContainer that resolves to the current tab's log view each frame.
        self._log_split = VSplit(
            [
                DynamicContainer(lambda: self.logview.window),
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

        # Network mode is a real two-pane transfer view: the local explorer pane
        # (the current tab's - see _sync_network_local_pane) sits on the left and
        # the remote browser on the right. DynamicContainer resolves the local 
        # pane per frame without inserting its window into two visible layouts.
        self.networkview.window.width = Dimension(min=0, preferred=0, weight=1)
        self._network_split = VSplit([
            DynamicContainer(self._network_local_container),
            Window(width=1, char=PANE_SEPARATOR, style="class:preview.border"),
            self.networkview.window,
        ])
        self._remote_shell_split = HSplit([
            self._network_split,
            Window(height=1, char="─", style="class:preview.border"),
            self.remote_shell.container,
        ])

        _net_pane_sep = Window(width=1, char=PANE_SEPARATOR, style="class:preview.border")

        def _with_network(inner):
            return VSplit([
                VSplit([inner], width=Dimension(weight=3)),
                _net_pane_sep,
                self.networkview.window,
            ])

        self._git_with_network = _with_network(git_area)
        self._log_with_network = _with_network(log_area)
        self._shell_with_network = HSplit([
            self._network_split,
            Window(height=1, char="─", style="class:preview.border"),
            self.shells.container,
        ])

        def _body():
            if self.mode == SEARCH:
                return self.search.container
            if self.mode == NOTES:
                return self.notesview.container
            if self.mode == SYSTEM:
                return self.systemview.container
            if self.mode == PREFERENCES:
                return self.preferencesview.container
            connected = self.networkview.connected
            if self.mode == NETWORK:
                # a stale network mode (the connection dropped from elsewhere)
                # falls back to the ordinary explorer, not a dead remote pane
                return self._network_split if connected else explorer_area
            if self.mode == REMOTE_SHELL:
                if not connected:  # same fallback as a stale network mode
                    return explorer_area
                return (self.remote_shell.container
                        if self.remote_shell_fullscreen()
                        else self._remote_shell_split)
            if self.mode == GIT:
                return self._git_with_network if connected else git_area
            if self.mode == LOG:
                return self._log_with_network if connected else log_area
            if self.mode == SHELL:
                # grow with output, then take the whole screen at the cap
                if self.shell_fullscreen():
                    return self.shells.container
                return self._shell_with_network if connected else self._shell_split
            # explorer: while connected the remote pane owns the right half, so
            # tha tab shows the same local|remote split as network mode
            return self._network_split if connected else explorer_area

        body = DynamicContainer(_body)

        # the action-menu float; its top is re-pointed at the cursor row each
        # time the menu opens (see _position_menu_float), defaulting to row 1.
        self._menu_float = Float(top=1, left=0, content=self.menu.container)

        root = FloatContainer(
            content=HSplit(
                [
                    Window(WheelScrollControl(
                        lambda d: None,  # the title bar doesn't scroll
                        on_click=self._on_title_click,  # click "nsh" -> F10 menu
                        text=self._title_text), height=1,
                        style="class:titlebar"),
                    body,
                    # outside shell mode, keep any open shells' tabs visible
                    # just above the status bar (click a tab to jump into it)
                    ConditionalContainer(
                        self.shells.overview_bar,
                        filter=Condition(lambda: self.mode != SHELL
                                         and self.shells.has_open_shell()),
                    ),
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
                # the action menu. Its top is set per-open to the cursor row
                # (see _position_menu_float); left=0 keeps it flush to the left
                # (the menu rows carry a one-space pad, landing text in column 1).
                # The default top=1 (just under the title bar) is the fallback.
                self._menu_float,
                # unpositioned Floats are centered on screen
                Float(content=self.dialog.container),
                Float(content=self.confirm_dialog.container),
                Float(content=self.about_dialog.container),
                Float(content=self.find_dialog.container),
                Float(content=self.chmod_dialog.container),
                Float(content=self.progress_dialog.container),
                Float(content=self.branch_browser.container),
            ],
        )

        application = Application(
            layout=Layout(root, focused_element=self.explorer.control),
            key_bindings=merge_key_bindings([
                load_key_bindings(), kb,
                DynamicKeyBindings(lambda: self._pane_kb)]),
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
        # fragments may be 2-tuples or (style, text, mouse_handler) 3-tuples
        used = sum(text_width(seg[1]) for seg in segs)
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

    @staticmethod
    def _clip_segs(segs, width, fill_style):
        """Truncate ``segs`` to ``width`` cells (cutting mid-segment if needed),
        then pad with ``fill_style`` so the result is exactly ``width`` wide."""
        out = []
        used = 0
        for style, text in segs:
            if used >= width:
                break
            w = text_width(text)
            if used + w <= width:
                out.append((style, text))
                used += w
            else:
                piece = cut_to_width(text, width - used)
                if piece:
                    out.append((style, piece))
                    used += text_width(piece)
                break
        if used < width:
            out.append((fill_style, " " * (width - used)))
        return out

    @staticmethod
    def _branch_seg(gs):
        """The coloured ``⎇ branch ±N`` title segment for ``gs`` (a GitStatus),
        or ``None`` when it's not a repo. Yellow when behind/ahead the upstream,
        red with uncommitted changes, else green (same precedence as the prompt)."""
        if not (gs and gs.is_repo and gs.branch):
            return None
        if gs.behind > 0:
            return "class:titlebar.branch.behind", f"⎇ {gs.branch} -{gs.behind}"
        if gs.dirty:
            return "class:titlebar.branch.dirty", f"⎇ {gs.branch}"
        if gs.ahead > 0:
            return "class:titlebar.branch.behind", f"⎇ {gs.branch} +{gs.ahead}"
        return "class:titlebar.branch", f"⎇ {gs.branch}"

    def _pane_git_segs(self, view):
        """Branch (+ in-progress merge/rebase) and selected-count badge for the
        explorer pane ``view``, from its own git status / selection - so each
        pane of the two-pane title, and the local half of the local|remote
        split, shows its own branch."""
        segs = []
        gs = view.git_status
        branch = self._branch_seg(gs)
        if branch:
            segs.append(("class:titlebar", " on "))
            segs.append(branch)
            if gs.in_progress:
                segs.append(("class:titlebar", " "))
                segs.append(("class:titlebar.branch.dirty", f"⚠ {gs.in_progress}"))
        sel = view.selected
        if sel:
            segs.append(("class:titlebar", "  "))
            segs.append(("class:titlebar.sel", f"● {len(sel)} selected"))
        return segs

    @staticmethod
    def _tail_to_width(s, width):
        """The trailing slice of ``s`` that fits in ``width`` display cells."""
        if width <= 0:
            return ""
        i, w = len(s), 0
        while i > 0:
            cw = char_width(s[i - 1]) or 1
            if w + cw > width:
                break
            w += cw
            i -= 1
        return s[i:]

    def _clip_path(self, s, width):
        """Clip a path to ``width`` cells keeping the *tail* (the current
        directory), with a leading ``…`` when truncated — so the two panes stay
        distinguishable even when their paths share a long common prefix."""
        if width <= 0:
            return ""
        if text_width(s) <= width:
            return s
        if width == 1:
            return "…"
        return "…" + self._tail_to_width(s, width - 1)

    # the current mode shown alongside the "nsh" label as "nsh|mode"
    _MODE_LABELS = {
        GIT: "git", LOG: "log", NOTES: "notes", SYSTEM: "system",
        NETWORK: "network", REMOTE_SHELL: "ssh shell",
        PREFERENCES: "preferences",
    }

    def _network_split_on_screen(self):
        """Whether _body() is showing the local|remote split at the top, so the
        title bar should align its halves to it.
        Mirrors _body(): the explorer and network modes show it whenever the
        connection is live, the local and remote shells keep it above their
        output until it goes full screen, and git / log keep their own 3:1
        column beside the remote pane instead (so they stay on the plain
        title). A stale network mode with no connection falls back too."""
        if not self.networkview.connected:
            return False
        if self.mode in (EXPLORER, NETWORK):
            return True
        if self.mode == SHELL:
            return not self.shell_fullscreen()
        if self.mode == REMOTE_SHELL:
            return not self.remote_shell_fullscreen()
        return False

    def _name_label(self):
        """The leading ``nsh`` label, suffixed with the active mode (e.g.
        ``nsh|git``) so the mode is visible right next to the program name."""
        mode = self._MODE_LABELS.get(self.mode)
        return f" nsh|{mode} " if mode else " nsh "

    def _title_text(self):
        # piggy-back on the per-second title repaint to pick up nshrc edits
        self._maybe_reload_config()
        # the "nsh" label adopts the menu's header colour while a *top* menu is
        # open (it sits right under the label, so the tint links the two);
        # cursor-anchored action menus appear elsewhere, so they leave it alone.
        tint = self.menu.active and not self._menu_at_cursor
        name_style = "class:menu.title" if tint else "class:titlebar.name"
        clock = [("class:titlebar.clock", f" {datetime.now().strftime('%H:%M:%S')} ")]
        if self._network_split_on_screen():
            try:
                total = get_app().output.get_size().columns
            except Exception:
                total = 80
            return self._network_title(name_style, clock, total)
        # the second explorer pane is never on screen while connected (the
        # remote pane has its column), so its half of the title isn't either
        if self.two_pane and not self.networkview.connected:
            return self._two_pane_title(name_style, clock)
        segs = [
            (name_style, self._name_label()),
            ("class:titlebar", " "),
            ("class:titlebar.path", shorten_home(self.cwd)),
        ]
        branch = self._branch_seg(self.git_status)
        if branch:
            segs.append(("class:titlebar", " on "))
            segs.append(branch)
            if self.git_status.in_progress:  # mid merge/rebase: flag it
                segs.append(("class:titlebar", " "))
                segs.append(("class:titlebar.branch.dirty",
                             f"⚠ {self.git_status.in_progress}"))
        selected = self.active_selection()
        if selected:
            segs.append(("class:titlebar", "   "))
            segs.append(("class:titlebar.sel", f"● {len(selected)} selected"))
        return self._fill_with_right(segs, "class:titlebar", clock)

    def _network_title(self, name_style, clock, total):
        """Network title aligned to its VSplit, with a blank separator cell."""
        sep = 1
        left_width = total // 2
        right_width = max(0, total - left_width - sep)
        clock_width = sum(text_width(text) for _style, text in clock)
        label = [(name_style, self._name_label()), ("class:titlebar", " ")]
        label_width = sum(text_width(text) for _style, text in label)
        local = self.networkview.local_view or self.explorer

        # the local half is a normal explorer pane, so it carries the same
        # branch / merge-in-progress / selected badge the other layouts show.
        # Like the two-pane title, the badge keeps its width and the path is
        # clipped to what is left, so the branch stays visible on narrow panes.
        local_git = self._pane_git_segs(local)
        local_git_width = sum(text_width(text) for _style, text in local_git)
        local_room = max(0, left_width - label_width - local_git_width)
        local_text = self._clip_path(shorten_home(local.cwd), local_room)
        left = label + [("class:titlebar.path", local_text)] + local_git
        left = self._clip_segs(left, left_width, "class:titlebar")

        remote_room = max(0, right_width - clock_width)
        remote_text = self._clip_path(self.networkview.location, remote_room)
        right = [("class:titlebar.path", remote_text)]
        right = self._clip_segs(right, remote_room, "class:titlebar")
        return left + [("class:titlebar", " ")] + right + clock

    def _two_pane_title(self, name_style, clock):
        """Title bar mirroring the two-pane layout: the left pane's path sits in
        the left half (after the ``nsh`` label), the right pane's path is aligned
        to the start of the right half, and the clock stays flush right. Each
        path is clipped to its half so the left can't bleed into the right and
        the right can't cover the clock."""
        try:
            total = get_app().output.get_size().columns
        except Exception:
            total = 80
        sep = 1  # the │ column between the two panes
        # the VSplit gives the left pane the extra column when the width is odd,
        # so its width is total // 2 and the separator sits at that column; the
        # right pane (and our right path) start at lw + 1.
        lw = total // 2                        # left pane / left half width
        rw = max(0, total - lw - sep)          # right half width (path + clock)
        clock_w = sum(text_width(t) for _, t in clock)

        def pane_region(i, avail):
            """Path segment for pane ``i`` followed by its own branch/selected
            badge, fitting ``avail`` cells: the branch keeps its width and the
            path is clipped to whatever's left (so the branch stays visible)."""
            git_segs = self._pane_git_segs(self.explorers[i])
            git_w = sum(text_width(t) for _, t in git_segs)
            path = self._clip_path(
                shorten_home(self.explorers[i].cwd),
                max(0, avail - git_w))
            return [("class:titlebar.path", path)] + git_segs

        # left half: nsh label + left pane path + its branch/selected
        label = [(name_style, self._name_label()), ("class:titlebar", " ")]
        label_w = sum(text_width(t) for _, t in label)
        left = label + pane_region(0, lw - label_w)
        left = self._clip_segs(left, lw, "class:titlebar")
        # right half: right pane path + its branch/selected, clipped so it stops
        # before the clock, then padded so the clock lands at the edge
        right = pane_region(1, rw - clock_w)
        right = self._clip_segs(right, max(0, rw - clock_w), "class:titlebar")
        return left + [("class:titlebar", " " * sep)] + right + clock

    @staticmethod
    def _fmt_key(spec):
        """Render a key spec (as written in nshrc) for the status bar / help:
        ``f7``->``F7``, ``c-p``->``^P``, ``s-tab``->``S-Tab``, space/esc named."""
        if not spec:
            return ""
        if spec == " ":
            return "Space"
        s = spec.strip()
        named = {"space": "Space", "escape": "ESC", "tab": "Tab", "enter": "↵"}
        if s.lower() in named:
            return named[s.lower()]
        low = s.lower()
        if len(low) >= 2 and low[0] == "f" and low[1:].isdigit():
            return low.upper()              # f7 -> F7
        if low.startswith("c-") and len(s) > 2:
            return "^" + s[2:].upper()      # c-p -> ^P
        if low.startswith("s-") and len(s) > 2:
            return "S-" + s[2:].capitalize()  # s-tab -> S-Tab
        if low.startswith(("a-", "m-")) and len(s) > 2:
            return "Alt+" + s[2:].upper()   # a-x -> Alt+X
        return s

    def _status_text(self):
        # the F7/F8-style pane keys are remappable; reflect the live spec
        pk = self._fmt_key(self.keys.get("pane_prev"))
        nk = self._fmt_key(self.keys.get("pane_next"))
        zk = self._fmt_key(self.keys.get("zoom"))
        queue_key = self._fmt_key(self.keys.get("queue_remove_last"))
        pane_pair = "/".join(p for p in (pk, nk) if p) or nk or pk
        # Hints are (key, label) or (key, label, action); an action makes the
        # hint clickable in the status bar. Directional / typing hints (arrows,
        # PgUp/PgDn, "type", history) have no single action, so they stay inert.
        ex = self.explorer
        if self.preview_focused() and self.mode in (EXPLORER, GIT, LOG):
            # the preview pane holds the focus: it scrolls with the arrows; Esc
            # (or a click) returns to the list — the pane keys now switch tabs
            hints = [
                ("PgUp/PgDn", "page"), ("g/G", "top/bottom"),
                (zk, "zoom", self.toggle_zoom),
                (":", "cmd", lambda: self.switch_mode(SHELL)),
                ("h/ESC", "list", self.focus_active_list),
            ]
            if self.preview.has_diff_hunks():
                hints[0] = (hints[0][0], "change")
                hints.insert(1, ("u", "revert change"))
                hints.insert(1, ("s", "stage/unstage"))
        elif self.mode == EXPLORER:
            hints = [
                ("Space", "select", ex.toggle_select),
                ("b", "marks", self.open_bookmark_menu),
                ("/", "find", self.enter_search),
                ("*", "select", ex.select_pattern),
                ("^N", "note", self.open_notes),
                (":", "cmd", lambda: self.switch_mode(SHELL)),
            ]
            # F7/F8 switch tabs; the 2-pane toggle sits beside it
            if len(self.shells.sessions) > 1:
                hints.append((pane_pair, "tab", self.shells.next))
            hints.append(("2", "1-pane" if self.two_pane else "2-pane",
                          self.toggle_two_pane))
            hints.append((zk, "zoom", self.toggle_zoom))
            hints.append(("q", "quit", self.exit))
        elif self.mode == SEARCH:
            hints = [
                ("type", "filter"),
                ("ESC", "cancel", self.cancel_search),
            ]
        elif self.mode == GIT:
            hints = [
                ("Space", "select", self.gitview.toggle_select),
                ("b", "marks", self.open_bookmark_menu),
                (zk, "zoom", self.toggle_zoom),
                (":", "cmd", lambda: self.switch_mode(SHELL)),
                ("ESC", "exit", self.close_git),
                ("q", "quit", self.exit),
            ]
        elif self.mode == LOG:
            hints = [
                ("/", "search", self.logview.search),
                ("n", "next", lambda: self.logview._find(1)),
                (zk, "zoom", self.toggle_zoom),
                ("ESC/q", "back", self.close_log),
            ]
        elif self.mode == NOTES:
            hints = [
                ("^S", "save", self.notesview.save_note),
                ("/", "search", self.notesview.start_search),
                ("y", "copy", self.notesview.copy_note), ("^V", "paste"),
                ("d/x", "delete", self.notesview.delete_note),
                ("u", "undo", self.notesview.undo_delete),
                ("ESC", "back", self.leave_notes),
            ]
        elif self.mode == SYSTEM:
            hints = [
                ("c/m/n", "sort cpu/mem/name"),
                ("v", "cmd/name", self.systemview.toggle_detail),
                ("/", "search", self.systemview.start_search),
                ("x", "kill", lambda: self.systemview.kill_selected()),
                ("r", "refresh", lambda: asyncio.ensure_future(self.systemview.refresh())),
                ("^N", "note", self.open_notes),
                ("ESC", "back", self.close_system),
            ]
        elif self.mode == PREFERENCES:
            hints = [
                ("type", "search"),
                ("^O", "edit nshrc", self.edit_preferences_file),
                ("ESC", "back", self.close_preferences),
            ]
        elif self.mode == NETWORK:
            nv = self.networkview
            if self.network_local_focused():
                hints = [
                    ("Space", "select", (nv.local_view or ex).toggle_select),
                    ("c", "upload", nv.upload),
                    ("Shift+L", "remote", lambda: self.focus_network_pane(1)),
                    ("ESC", "clear selection"),
                ]
            else:
                if nv._preview_entry is not None:
                    hints = [
                        ("c", "download", nv.download),
                        ("Shift+H", "local", lambda: self.focus_network_pane(-1)),
                        ("ESC", "files", nv.close_preview),
                    ]
                else:
                    hints = [
                        ("Space", "select", nv.toggle),
                        ("c", "download", nv.download),
                        ("n", "mkdir", nv.new_dir),
                        ("s", "sort", nv.open_sort_menu),
                        ("/", "find", nv.start_search),
                        ("Shift+H", "local", lambda: self.focus_network_pane(-1)),
                        ("ESC", "clear selection", nv.cancel),
                        ("q", "quit", self.exit),
                    ]
        elif self.mode == REMOTE_SHELL:
            hints = [
                ("ESC", "files", lambda: self.switch_mode(NETWORK)),
            ]
            if queue_key:
                hints.insert(0, (queue_key, "unqueue", self.remove_last_queued))
        else:
            hints = [
                ("^T", "new tab", self.shells.new_session),
                ("F2", "rename tab", self.shells.rename),
                (f"Alt+←→/{pk}·{nk}", "switch"),
                ("PgUp/PgDn", "scroll"),
            ]
            if queue_key:
                hints.append((queue_key, "unqueue", self.remove_last_queued))
            hints.extend([
                ("^W", "close tab", self.close_shell_tab),
                ("ESC", "back", self.leave_shell),
            ])
        segs = []
        # a yellow square + the note count at the very front whenever there are
        # saved notes — ahead of the message and the shortcut hints; clicking it
        # opens notes (like Ctrl+N)
        note_count = len(self.notesview.notes)
        if note_count > 0:
            segs.append(("class:statusbar.notes", f" ■ {note_count} ",
                         self._hint_click(self.open_notes)))
        # how many commands are waiting in this tab's queue (shell mode only); the
        # commands themselves are listed in grey above the prompt, not here
        if self.mode == SHELL:
            queued = len(self.shell.pending)
            if queued > 0:
                segs.append(("class:statusbar.queue", f" ⋯ {queued} queued "))
        # the message sits in front of the shortcuts and stays until it's
        # explicitly cleared (directory change, mode change, or ESC)
        if self.message:
            segs.append(("class:statusbar.msg", f" {self.message} "))
        for hint in hints:
            key, label = hint[0], hint[1]
            action = hint[2] if len(hint) > 2 else None
            if action is not None:
                handler = self._hint_click(action)
                segs.append(("class:statusbar.key", f" {key} ", handler))
                segs.append(("class:statusbar", f"{label} ", handler))
            else:
                segs.append(("class:statusbar.key", f" {key} "))
                segs.append(("class:statusbar", f"{label} "))
        return self._fill(segs, "class:statusbar")

    def _hint_click(self, action):
        """A status-bar hint's mouse handler: run ``action`` on a click. While a
        popup menu is up the click dismisses it instead (a dialog swallows it),
        matching the click-outside-to-close behaviour elsewhere."""
        def handler(mouse_event):
            if mouse_event.event_type != MouseEventType.MOUSE_DOWN:
                return None
            if self._overlay_active():
                self.consume_menu_click()  # dismiss a menu; dialogs stay modal
                return None
            action()
            return None
        return handler

    # -- modes ----------------------------------------------------------------
    def toggle_mode(self):
        if self.mode == SHELL:
            self.leave_shell()
        else:
            self.switch_mode(SHELL)

    def leave_shell(self):
        self.switch_mode(self._shell_return)
        if self._shell_return == NETWORK:
            self.focus_network_pane(self._shell_return_pane)

    def switch_mode(self, mode):
        # the remote views need a live connection: a request left over from
        # before a disconnect (a remembered "return tdo network" mode, say)
        # lands in the explorer instead of an empty remote pane
        if mode in (NETWORK, REMOTE_SHELL) and not self.networkview.connected:
            mode = EXPLORER
        # while connected the explorer *is* the local half of the local|remote
        # split, so it enters network mode - with the focus on the local list
        # rather than the remote one
        local_half = mode == EXPLORER and self.networkview.connected
        if local_half:
            mode = NETWORK
        from_mode = self.mode
        if mode == SHELL and self.mode in (EXPLORER, GIT, NETWORK):
            self._shell_return = self.mode
            if self.mode == NETWORK:
                self._shell_return_pane = self._network_pane_direction()
        if from_mode != mode:
            self.message = ""  # a mode change dismisses the status message
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
        elif mode == NOTES:
            self.notesview.load()
            self.notesview.focus_input()
        elif mode == SYSTEM:
            self.systemview.start()
            self.application.layout.focus(self.systemview.list_control)
        elif mode == PREFERENCES:
            self.preferencesview.start()
            self.application.layout.focus(self.preferencesview.query_control)
        elif mode == NETWORK:
            self.application.layout.focus(
                (self.networkview.local_view or self.explorer).control
                if local_half else self.networkview.control)
        elif mode == REMOTE_SHELL:
            self.application.layout.focus(self.remote_shell.buffer)
        else:
            self.application.layout.focus(self.explorer.control)
        self.invalidate()

    def toggle_git_mode(self):
        if self.mode == GIT:
            self.close_git()
            return
        if not self.git_status.is_repo:
            self.set_message("not a git repository")
            return
        self._git_return = NETWORK if self.mode == NETWORK else EXPLORER
        if self.mode == NETWORK:
            self._git_return_pane = self._network_pane_direction()
        self.switch_mode(GIT)

    def close_git(self):
        self.switch_mode(self._git_return)
        if self._git_return == NETWORK:
            self.focus_network_pane(self._git_return_pane)

    def open_log(self, paths=None):
        if not self.git_status.is_repo:
            self.set_message("not a git repository")
            return
        self.logview.path_filters = tuple(Path(path) for path in (paths or ()))
        self._log_return = (self.mode if self.mode in (EXPLORER, GIT, NETWORK)
                            else EXPLORER)
        if self.mode == NETWORK:
            self._log_return_pane = self._network_pane_direction()
        self.switch_mode(LOG)

    def close_log(self):
        self.switch_mode(self._log_return)
        if self._log_return == NETWORK:
            self.focus_network_pane(self._log_return_pane)

    # -- find (text / file) ---------------------------------------------------
    def open_find(self):
        """Find: choose between searching file *contents* (grep) or file *names*
        (the fuzzy finder)."""
        self.open_menu("Find", [
            ("Text (grep)", self.find_text),
            ("File (fuzzy)", lambda: self.enter_search()),
        ])

    def find_text(self):
        self._find_return = self.mode
        if self.mode == NETWORK:
            self._find_return_pane = self._network_pane_direction()
        self._capture_dialog_focus()
        self.find_dialog.open(self._run_grep)
        self.application.layout.focus(self.find_dialog.control)
        self.invalidate()

    def _run_grep(self, phrase, case_sensitive, whole_word):
        """Build and run a grep over the current directory tree from the find
        dialog's phrase and options, streaming results into the shell."""
        if not phrase.strip():
            return
        # -r recurse, -n line numbers, -I skip binaries. The phrase is a grep
        # pattern. (We deliberately avoid -F: GNU grep 3.0, shipped with Git
        # Bash, crashes on `-F -r`.) --color=always so grep still emits ANSI
        # colour through nsh's pipe — its stdout isn't a TTY, where it would
        # otherwise turn colour off.
        flags = "-rnI"
        if not case_sensitive:
            flags += "i"
        if whole_word:
            flags += "w"
        cmd = f"grep --color=always {flags} -e {shlex.quote(phrase)} ."
        self.switch_mode(SHELL)
        self._shell_return = self._find_return
        self._shell_return_pane = self._find_return_pane
        self.run_in_shell(self.shell, cmd)

    # -- notes ----------------------------------------------------------------
    def open_notes(self):
        self._notes_return = self.mode
        if self.mode == NETWORK:
            self._notes_return_pane = self._network_pane_direction()
        self.switch_mode(NOTES)

    def leave_notes(self):
        """Leave notes mode, auto-saving any unsaved draft in the editbox (no
        prompt) so nothing is lost on the way out."""
        dest = getattr(self, "_notes_return", EXPLORER)
        if self.notesview.input.text.strip():
            self.notesview.save_note()
        self.switch_mode(dest)
        if dest == NETWORK:
            self.focus_network_pane(self._notes_return_pane)

    # -- system (process manager) ---------------------------------------------
    def open_system(self):
        self._system_return = self.mode
        if self.mode == NETWORK:
            self._system_return_pane = self._network_pane_direction()
        self.switch_mode(SYSTEM)

    def close_system(self):
        self.switch_mode(self._system_return)
        if self._system_return == NETWORK:
            self.focus_network_pane(self._system_return_pane)

    # -- fuzzy search ---------------------------------------------------------
    def enter_search(self, query=""):
        self._search_remote = False
        self._search_return = NETWORK if self.mode == NETWORK else EXPLORER
        self._pending_query = query
        self.switch_mode(SEARCH)

    def enter_network_search(self, query=""):
        self._search_remote = True
        self._search_return = NETWORK
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
        self.switch_mode(self._search_return)
        if self._search_return == NETWORK:
            # A local search started from the left half of Network mode should
            # restore that pane, not the remote pane selected by switch_mode's
            # normal Network default.
            self.focus_network_pane(-1)

    def cancel_search(self):
        if self.picker:
            self.search_result = None
            self.exit()
            return
        if self._search_remote:
            # Stop the background remote index walk so the next '/' press can
            # start a fresh search instead of being blocked by `indexing`
            self.networkview.cancel_indexing()
        self.switch_mode(self._search_return)
        if self._search_return == NETWORK:
            self.focus_network_pane(1 if self._search_remote else -1)

    def toggle_preview(self):
        self.show_preview = not self.show_preview
        self.invalidate()

    def preview_focused(self):
        try:
            return self.application.layout.has_focus(self.preview.control)
        except Exception:
            return False

    def _active_list_control(self):
        """The list control beside the preview in the current mode."""
        if self.mode == GIT:
            return self.gitview.control
        if self.mode == LOG:
            return self.logview.control
        return self.explorer.control

    def focus_active_list(self):
        self.application.layout.focus(self._active_list_control())
        self.invalidate()

    def _preview_on_screen(self):
        """Whether the preview pane is actually laid out beside the list. While
        a remote connection is up it isn't: the remote pane holds the right half
        of the explorer, network and shell views - only the git / log diff
        preview keeps its column."""
        if self.networkview.connected and self.mode not in (GIT, LOG):
            return False
        return self.show_preview and self._wide_enough()

    def toggle_preview_focus(self):
        """Move focus between the list and the preview pane (explorer / git /
        log). Does nothing when the preview isn't actually on screen."""
        if not self._preview_on_screen():
            return
        if self.preview_focused():
            self.focus_active_list()
        else:
            self.preview.focus()

    def focus_preview(self):
        """Move focus to the preview pane when it's actually on screen. Used by
        Right on a non-expandable file (explorer) and on a changed file (the
        git / log diff). A no-op otherwise, so the key stays inert when there's
        no preview to step into."""
        if not self._preview_on_screen():
            return
        # two-pane explorer replaces the preview with the second pane; git / log
        # always show the preview regardless of the explorer's two-pane flag
        if self.mode == EXPLORER and self.two_pane:
            return
        self.preview.focus()

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

    def remote_shell_fullscreen(self):
        """Match the local shell: maximize only after remote output fills split."""
        cap = self._shell_cap()
        return self.remote_shell.display_rows(
            self._term_cols(), limit=cap) > cap

    def remote_shell_split_output_rows(self):
        cap = self._shell_cap()
        return max(0, min(self.remote_shell.display_rows(
            self._term_cols(), limit=cap), cap))

    # -- command line ---------------------------------------------------------
    def run_in_shell(self, session, cmd):
        """Run ``cmd`` typed in ``session``.

        If that session is still running a command, queue the new command in the
        same tab. It runs once the current one — and any commands already ahead
        of it — finishes.

        A leading ``!`` forces the rest of the line onto the real terminal
        (run_in_term) — an escape hatch for interactive tools nsh doesn't
        auto-detect — skipping the builtins and the streaming pipe.
        """
        if not cmd.strip():
            return
        if session.busy():
            session.pending.append(cmd)
            self.invalidate()
            return
        self._dispatch_command(session, cmd)

    def run_shell_batch(self, session, commands):
        """Run pasted commands in order without racing the busy transition."""
        commands = [command for command in commands if command.strip()]
        if not commands:
            return
        if session.busy():
            session.pending.extend(commands)
        else:
            first, *rest = commands
            session.pending.extend(rest)
            self.run_in_shell(session, first)
        self.invalidate()

    def paste_shell_text(self, text, remote=False):
        """Insert a one-line paste, or submit a multi-line paste as a batch."""
        shell = self.remote_shell if remote else self.shell
        buffer = shell.command_buffer
        normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
        if "\n" not in normalized:
            buffer.insert_text(normalized)
            return

        document = buffer.document
        combined = (document.text_before_cursor + normalized
                    + document.text_after_cursor)
        commands = [line for line in combined.split("\n") if line.strip()]
        for command in commands:
            buffer.history.append_string(command)
        buffer.reset()
        shell.command_window.horizontal_scroll = 0
        if remote:
            shell.run_batch(commands)
        else:
            self.run_shell_batch(shell, commands)
        self.invalidate()

    def _dispatch_command(self, session, cmd):
        """Actually run ``cmd`` in ``session`` (no busy check). When a builtin
        finishes synchronously, drain the next queued command right away; an
        async command drains its queue from :meth:`_exec` on completion."""
        explicit_bang = cmd.lstrip().startswith("!")
        force_term = explicit_bang or self._external_command(cmd)
        if explicit_bang:
            cmd = cmd.lstrip()[1:].strip()
            if not cmd:
                self._drain_pending(session)
                return
        # a `source FILE` line: each command runs in its own subprocess, so note
        # the file and re-source it ahead of every later command (CommandRunner)
        src = session.runner.sourced_file(cmd)
        if src:
            self._record_source(src)
        # echo the command first: it bakes the previous command's run-time badge
        # into the scrolled-up line, so reset only clears the live prompt below.
        session.append_command(cmd)
        session.runner.reset_result()  # clear the previous command's status tint
        if force_term:
            asyncio.ensure_future(self._exec(session, cmd, force_term=True))
        elif not self._handle_builtin(session, cmd):
            asyncio.ensure_future(self._exec(session, cmd))
        else:
            self._drain_pending(session)  # builtin done; run the next queued one

    def _external_command(self, command):
        """Whether ``command`` is configured to run on the real terminal.

        ``external_commands`` accepts command names separated by whitespace or
        commas. Only the executable token is compared, so options and arguments
        do not affect the match; paths such as ``C:\\tools\\foo.exe`` match
        ``foo.exe``.
        """
        configured = self.settings.get("external_commands", "")
        names = {name.casefold() for name in configured.replace(",", " ").split()
                 if name}
        if not names:
            return False
        try:
            parts = shlex.split(command, posix=os.name != "nt")
        except ValueError:  # incomplete quote: let the shell report the error
            parts = command.strip().split(maxsplit=1)
        if not parts:
            return False
        executable = parts[0].strip("\"'")
        return os.path.basename(executable).casefold() in names

    def _drain_pending(self, session):
        """Start the next queued command for ``session``, if the tab is now
        free. Called when a command finishes (async) or a builtin returns."""
        if session.pending and not session.busy():
            self._dispatch_command(session, session.pending.pop(0))
            self.invalidate()  # the queue shrank: refresh the list / count

    def _record_source(self, raw):
        """Remember a `source`d file (resolved to an absolute path) so it's
        re-sourced before later commands. Skipped if the file doesn't exist."""
        path = os.path.expanduser(raw)
        if not os.path.isabs(path):
            path = os.path.join(str(self.cwd), path)
        path = os.path.normpath(path)
        if os.path.isfile(path) and path not in self.sourced_files:
            self.sourced_files.append(path)

    def _handle_builtin(self, session, cmd):
        stripped = cmd.strip()
        if stripped in ("exit", "quit"):
            self.shells.close(session)  # close this tab (or leave shell mode)
            return True
        if stripped in ("clear", "cls"):
            session.clear()
            return True
        if stripped == "cd" or stripped.startswith("cd "):
            target = _unquote_arg(stripped[2:].strip()) or "~"
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

    async def _exec(self, session, cmd, force_term=False):
        runner = session.runner
        try:
            if force_term:
                await runner.run_in_term(cmd)  # a leading '!' forced this
            elif runner.is_git_network(cmd):
                await self._exec_git_network(session, runner, cmd)
            elif runner.is_interactive(cmd):
                await runner.run_in_term(cmd)
            else:
                await runner.run(cmd)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            session.append(f"nsh: {exc}", "class:shell.error")
        self.invalidate()
        self._drain_pending(session)  # this tab is free: run the next queued one

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
        self.explorer.cwd = target  # the active pane owns the directory
        self._remember_drive(target)  # so a later "X:" returns to here
        self.explorer.selected.clear()
        self.explorer.expanded.clear()  # the tree is relative to the old cwd
        self.explorer.load()
        # put the cursor on ``select_name`` (e.g. the directory we came up from),
        # else the first real row (past the leading "..")
        self.explorer.cursor = self.explorer.first_index()
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

    def _visible_panes(self):
        """The explorer panes actually on screen.

        While connected the left half is the network view's own local pane, and
        the second explorer pane is not on screen at all (the remote pane owns
        that column). Anything that keeps a pane fresh has to follow what is
        displayed rather than the tab's ``active_pane``: the two normally agree,
        but nothing enforces it, and when they drift the listing you are looking
        at silently stops updating while a hidden pane is refreshed in its place.
        """
        if self.networkview.connected:
            return [self.networkview.local_view or self.explorer]
        if self.two_pane:  # so the inactive pane's markers stay correct too
            return list(self.explorers)
        return [self.explorer]

    def _git_panes(self):
        """The panes whose git status should be kept fresh: the visible ones."""
        return self._visible_panes()

    def schedule_git(self):
        if self._git_task and not self._git_task.done():
            self._git_task.cancel()
        # reset the active pane while its query runs; the other pane keeps its
        # current markers until its own query returns (no flicker on nav)
        self.explorer.git_status = git.GitStatus()
        targets = [(ex, ex.cwd, self._child_directories(ex))
                   for ex in self._git_panes()]
        self._git_task = asyncio.ensure_future(self._git_worker(targets))

    @staticmethod
    def _child_directories(explorer):
        """Real directories currently visible, including expanded descendants."""
        return tuple(entry.path for entry in explorer.entries
                     if entry.is_dir and not entry.is_parent)

    async def _git_worker(self, targets):
        for ex, path, children in targets:
            status = await git.query(path, children)
            if path == ex.cwd:  # ignore results for a directory the pane left
                ex.git_status = status
        self.gitview.on_status_changed()
        self.invalidate()

    async def refresh_git(self):
        # refresh every visible pane (a two-pane copy/move changes the other
        # pane's directory too)
        for ex in self._git_panes():
            ex.git_status = await git.query(ex.cwd, self._child_directories(ex))
            ex._git_signature = ex._git_watch_signature(ex.entries)
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
        elif self.mode == NOTES:
            self.notesview.focus_input()
        elif self.mode == SYSTEM:
            self.application.layout.focus(self.systemview.list_control)
        elif self.mode == PREFERENCES:
            self.application.layout.focus(self.preferencesview.query_control)
        elif self.mode == NETWORK:
            self.focus_network_pane(
                getattr(self.shells.current(), "network_pane", -1))
        elif self.mode == REMOTE_SHELL:
            self.application.layout.focus(self.remote_shell.buffer)
        elif self.mode == SEARCH:
            self.application.layout.focus(self.search.query_buffer)
        else:
            self.application.layout.focus(self.explorer.control)

    # -- confirmation dialog --------------------------------------------------
    def confirm(self, label, callback):
        """Show a centered yes/no dialog; ``callback(True|False)`` on resolve."""
        self._capture_dialog_focus()
        self.confirm_dialog.open("Confirm", label, callback)
        self.application.layout.focus(self.confirm_dialog.control)
        self.invalidate()

    def open_progress_dialog(self, title, label, on_cancel):
        self._capture_dialog_focus()
        self.progress_dialog.open(title, label, on_cancel)
        self.application.layout.focus(self.progress_dialog.control)
        self.invalidate()

    def update_progress_dialog(self, done, total):
        self.progress_dialog.update(done, total)
        self.invalidate()

    def close_progress_dialog(self):
        self.progress_dialog.close()

    def open_chmod_dialog(self, title, mode, on_accept):
        """Show the permission grid seeded with ``mode``; ``on_accept(mode_int)``
        gets the chosen 0-0o777 value."""
        self._capture_dialog_focus()
        self.chmod_dialog.open(title, mode, on_accept)
        self.application.layout.focus(self.chmod_dialog.control)
        self.invalidate()

    def open_branch_browser(self, rev):
        """Open the read-only file-tree browser for branch (or ref) ``rev``."""
        self.branch_browser.open(rev)
        self.application.layout.focus(self.branch_browser.control)
        self.invalidate()

    # -- action menu ----------------------------------------------------------
    def _active_list_window(self):
        """The list Window whose cursor row the menu should drop from, or None
        in modes without one (shell, search, …) — there the menu uses its
        default top-of-screen position."""
        if self.mode == GIT:
            return self.gitview.window
        if self.mode == LOG:
            return self.logview.window
        if self.mode == EXPLORER:
            return self.explorer.window
        return None

    def _menu_name_end_col(self):
        """The column just past the cursor item's text in the active list — the
        filename (explorer / git) or the commit line (log) — so the menu can be
        offset right of it and keep that content visible."""
        if self.mode == GIT:
            return self.gitview.cursor_name_end_col()
        if self.mode == LOG:
            return self.logview.cursor_name_end_col()
        if self.mode == EXPLORER:
            return self.explorer.cursor_name_end_col()
        return 0

    def _menu_width(self, title, items):
        """The menu's rendered width in cells (mirrors util/menu.py): the widest
        of the title, the labels, and a 12-cell floor, plus the row's own padding."""
        labels = [lbl for lbl, _ in items if lbl is not SEPARATOR]
        inner = max([text_width(title) + 8]
                    + [text_width(lbl) + 2 for lbl in labels] + [12])
        return inner + 2  # one-space pad on each side of every row

    def _position_menu_float(self, title, items):
        """Drop the action menu from the cursor row instead of the top, and shift
        it right past the cursor item's text (filename / commit line) so that
        content stays visible. Reads the active list window's last render for the
        cursor's absolute screen row; the top is clamped to stay above the status
        bar and the left so the whole menu still fits on the right (a long log
        line therefore keeps as much visible as the menu width allows). Any
        failure (no render yet, or a prompt_toolkit internal change) falls back to
        the top-left."""
        top, left = 1, 0
        win = self._active_list_window()
        info = getattr(win, "render_info", None) if win is not None else None
        if info is not None:
            try:
                size = self.application.output.get_size()
                rows, cols = size.rows, size.columns
                # cursor_position is relative to the window; _y_offset / _x_offset
                # are the window's absolute top / left, so adding them gives the
                # absolute screen position.
                cursor_row = info.cursor_position.y + info._y_offset
                height = min(len(items) + 1, max(1, rows - 2))  # title + items
                top = max(1, min(cursor_row, rows - height - 1))
                # shift right past the item's text (+1 gap). The offset is capped
                # at the pane width — a long (or truncated) name can't extend past
                # the pane, so the menu's x must not either — then made absolute
                # and clamped so the whole menu still fits on screen.
                rel = min(self._menu_name_end_col() + 1, info.window_width)
                end = rel + info._x_offset
                left = max(0, min(end, max(0, cols - self._menu_width(title, items))))
            except Exception:  # noqa: BLE001 - any internal change: use defaults
                top, left = 1, 0
        self._menu_float.top = top
        self._menu_float.left = left

    def open_menu(self, title, items, on_close=None, at_cursor=False):
        """Open the popup menu. By default it sits at the top, under the "nsh"
        label (where every menu used to be). The file/item action menus pass
        ``at_cursor=True`` to drop from the cursor row instead."""
        items = list(items)  # materialize once (may be a generator) before counting
        # Menus temporarily own focus. Remember the exact originating control so
        # a local-pane menu in Network mode does not fall back to the remote pane.
        self._menu_return_focus = self.application.layout.current_control
        self._menu_at_cursor = at_cursor
        if at_cursor:
            self._position_menu_float(title, items)
        else:
            self._menu_float.top, self._menu_float.left = 1, 0
        self.menu.open(title, items, on_close)
        self.application.layout.focus(self.menu.control)
        self.invalidate()

    def _menu_closed(self):
        control, self._menu_return_focus = self._menu_return_focus, None
        try:
            if control is None:
                raise ValueError("no menu return focus")
            self.application.layout.focus(control)
        except Exception:  # noqa: BLE001 - stale/hidden control: mode fallback
            self._restore_focus()
        self.invalidate()

    # -- mouse ----------------------------------------------------------------
    def consume_menu_click(self):
        """When a popup menu is open, a click on any background pane dismisses it
        (like Esc) and is otherwise ignored. Returns True when it did so, so the
        caller skips its own click handling. Clicks inside the menu go to the
        menu's own float and never reach here."""
        if self.menu.active:
            self.menu.close()
            return True
        return False

    def _on_title_click(self, mouse_event):
        """A click on the leading ``nsh`` label opens the nsh (F10) menu; the
        rest of the title bar is inert. With a menu open the click dismisses it."""
        if self.consume_menu_click():
            return
        if self._overlay_active():
            return  # a dialog is up: leave the title inert
        if mouse_event.position.x < text_width(self._name_label()):
            self.open_nsh_menu()

    def double_click(self, tag, index):
        """Record a left-click and report whether it completes a double-click —
        a second click on the same ``(tag, index)`` within 400 ms. The list
        views use this to open on a double-click while a single click only moves
        the cursor."""
        now = time.monotonic()
        prev = self._last_click
        self._last_click = (tag, index, now)
        return bool(prev and prev[0] == tag and prev[1] == index
                    and now - prev[2] <= 0.4)

    def close_shell_if_open(self):
        """A click on the explorer or preview while the shell is focused leaves
        shell mode (closing it) so the focus can move to the clicked pane."""
        if self.mode == SHELL:
            self.switch_mode(EXPLORER)

    def focus_pane(self, view):
        """Make the clicked explorer ``view`` the active pane (in two-pane mode)
        and focus it; the cwd / git status / shell follow it as with the keys.
        Clicking a pane while the shell is focused also closes the shell."""
        self.close_shell_if_open()
        # a mouse click that moves to a different pane cancels zoom (rather than
        # handing the big 9:1 share to the clicked pane, the way the keys do)
        if self.zoom and not self.application.layout.has_focus(view.control):
            self.zoom = False
        try:
            idx = self.explorers.index(view)
        except ValueError:
            idx = self.active_pane
        if self.two_pane and idx != self.active_pane:
            self.active_pane = idx
            self._sync_network_local_pane()
            try:
                os.chdir(self.explorer.cwd)  # process cwd follows the active pane
            except OSError:
                pass
            self.message = ""
            self.preview.clear()
        self.application.layout.focus(view.control)

    # -- two-pane view --------------------------------------------------------
    def switch_pane(self):
        """Move the cursor to the other explorer pane (two-pane mode). The cwd,
        git status, title bar and shell all follow the newly active pane."""
        if not self.two_pane:
            return
        self.switch_to_pane(self.active_pane ^ 1)  # two panes: toggle

    def move_pane_focus(self, direction):
        """Shift+H / Shift+L: move focus left (``direction`` < 0) or right
        (> 0) across the on-screen columns. In two-pane view those are the two
        explorer panes; in single-pane view they are the list and its preview
        (so Shift+L steps into the preview and Shift+H steps back)."""
        if self.mode == NETWORK:
            self.focus_network_pane(direction)
            return
        if self.two_pane:  # [left pane, right pane]; no preview in this layout
            self.switch_to_pane(1 if direction > 0 else 0)
            return
        # Opening the cursor directory as a new right-hand pane is strictly an
        # Explorer single-pane gesture. Other modes can reuse explorer controls
        # (notably the local half of Network/SSH), but must never create/replace
        # a pane as a side effect of moving focus.
        if self.mode != EXPLORER:
            return
        # single pane: Shift+L on a directory opens it beside the current one
        # (enters two-pane view with the right pane in that directory) instead
        # of merely focusing its preview; otherwise Shift+H/L step between the
        # list and the preview when it's on screen.
        if direction > 0 and not self.preview_focused():
            entry = self.explorer.current()
            if entry is not None and entry.is_dir and not entry.is_parent:
                self.open_dir_in_two_pane(entry.path)
                return
            if self._preview_on_screen():
                self.preview.focus()
                self.invalidate()
        elif direction < 0 and self.preview_focused():
            self.focus_active_list()

    def network_local_focused(self):
        """Whether focus is on the local half of the Network transfer view."""
        local = self.networkview.local_view
        if local is None or not hasattr(self, "application"):
            return False
        try:
            return self.application.layout.has_focus(local.control)
        except Exception:
            return False

    def _network_pane_direction(self):
        """Pane to restore after a full-screen transient view: -1 local, +1 remote."""
        return -1 if self.network_local_focused() else 1

    def focus_network_pane(self, direction):
        """Shift+H/L focus the local/remote halves of Network mode."""
        if self.mode != NETWORK:
            return
        local = self.networkview.local_view or self.explorer
        target = local.control if direction < 0 else self.networkview.control
        self.application.layout.focus(target)
        self.invalidate()

    def switch_to_pane(self, idx):
        """Make pane ``idx`` (0 = left, 1 = right) the active one in two-pane
        mode via the keyboard. The cwd, git status, title bar and shell all
        follow it; a no-op outside two-pane mode or when it's already active."""
        if not self.two_pane or idx == self.active_pane:
            return
        self.active_pane = idx
        self._sync_network_local_pane()
        try:
            os.chdir(self.explorer.cwd)  # the process cwd follows the active pane
        except OSError:
            pass
        self.message = ""
        self.preview.clear()
        # both panes already carry their own git status, so just re-focus and
        # repaint — no re-query needed
        self.application.layout.focus(self.explorer.control)
        self.invalidate()

    # -- zoom (9:1 split) -----------------------------------------------------
    # the focused pane's width weight while zoomed; the other pane stays at 1
    _ZOOM_WEIGHT = 9

    def _overlay_active(self):
        """True while any popup (menu / dialog) is up — keys shouldn't fall
        through to the panes then."""
        return (self.menu.active or self.dialog.active or self.confirm_dialog.active
                or self.about_dialog.active or self.find_dialog.active
                or self.chmod_dialog.active or self.progress_dialog.active)

    def _zoom_active(self):
        """Zoom only reshapes a split that's actually on screen: the explorer,
        git and log views (not the shell's overlaid listing)."""
        return self.zoom and self.mode in (EXPLORER, GIT, LOG)

    def _pane_dim(self, focused):
        """The width Dimension for a split pane: the focused one wins the 9:1
        share while zoomed, otherwise everything is weight 1 (an even split)."""
        weight = self._ZOOM_WEIGHT if (self._zoom_active() and focused) else 1
        return Dimension(min=0, preferred=0, weight=weight)

    def _explorer_focused(self, i):
        """Whether explorer pane ``i`` is the one zoom should enlarge: the active
        pane in two-pane view, else pane 0 unless the preview holds the focus."""
        if self.two_pane:
            return self.active_pane == i
        return i == 0 and not self.preview_focused()

    def list_cols(self, view, total):
        """Columns the explorer listing ``view`` actually occupies, so it can
        right-align its size column. Mirrors the VSplit's weights — including
        zoom — instead of assuming an even split, which would leave the size
        column stranded mid-pane once a pane is zoomed wide."""
        avail = max(1, total - 1)  # minus the | separator column
        if self.networkview.connected:
            # local | remote: the lone local list shares the width with the
            # remote pane, whatever the tab's own two-pane flag says - an even
            # split, or the 3:1 one the shell's overlaid listing sits in
            w_local = self._pane_dim(
                self._explorer_focused(self.active_pane)).weight
            return max(4, round(avail * w_local / (w_local + 1)))
        # no split on screen: the listing owns the whole width
        if not self.two_pane and not (self.show_preview and self._wide_enough()):
            return total
        idx = 0 if view is self.explorers[0] else 1
        w_self = self._pane_dim(self._explorer_focused(idx)).weight
        if self.two_pane:
            w_other = self._pane_dim(self._explorer_focused(1 - idx)).weight
        else:
            w_other = self._pane_dim(self.preview_focused()).weight
        return max(4, round(avail * w_self / (w_self + w_other)))

    def toggle_zoom(self):
        """Toggle the 9:1 zoom. The enlarged pane follows the focus, so moving
        focus (the pane keys) hands the space to the newly focused pane."""
        self.zoom = not self.zoom
        self.invalidate()

    def open_dir_in_two_pane(self, path):
        """Shift+L on a directory in single-pane view: enter two-pane mode with
        the other pane opened at ``path`` and focused, so it reads as stepping
        into the directory beside the current one rather than just previewing
        it. Mirrors :meth:`toggle_two_pane`'s second-pane setup."""
        if self.networkview.connected:  # the remote pane owns the right half
            self.set_message("two-pane is unavailable while connected")
            return
        self.two_pane = True
        other = self.explorers[1 - self.active_pane]
        other.cwd = Path(path)
        other.selected.clear()
        other.expanded.clear()
        other.cursor = 0
        other.load()
        self.active_pane = 1 - self.active_pane  # focus the stepped-into pane
        try:
            os.chdir(self.explorer.cwd)  # the process cwd follows the active pane
        except OSError:
            pass
        self.message = ""
        self.schedule_git()  # query git for both now-visible panes
        self.preview.clear()
        self.application.layout.focus(self.explorer.control)
        self.invalidate()

    def toggle_two_pane(self):
        if self.networkview.connected:
            # the remote pane holds the right half; a second local pane would
            # be a third column, so the toggle waits for the disconnect
            self.set_message("two-pane is unavailable while connected")
            return
        self.two_pane = not self.two_pane
        if self.two_pane:
            # open the second pane at the active pane's directory; navigate it
            # independently from there
            other = self.explorers[1 - self.active_pane]
            other.cwd = self.explorer.cwd
            other.selected.clear()
            other.expanded.clear()
            other.cursor = 0
            other.load()
        elif self.active_pane != 0:
            # single-pane shows pane 0, so make it the active one
            self.active_pane = 0
            try:
                os.chdir(self.explorer.cwd)
            except OSError:
                pass
        # query git for the now-visible pane set (both panes when entering
        # two-pane, so the second pane's markers appear)
        self.schedule_git()
        self.preview.clear()
        self.application.layout.focus(self.explorer.control)
        self.invalidate()

    # -- nsh menu (F10) -------------------------------------------------------
    def open_nsh_menu(self):
        network = getattr(self, "networkview", None)
        network_item = (
            ("Network: Disconnect", network.disconnect)
            if network is not None and network.connected
            else ("Network", self.open_network_menu)
        )
        self.open_menu("nsh", [
            ("Bookmarks", self.open_bookmark_menu),
            ("Find", self.open_find),
            network_item,
            (SEPARATOR, None),
            ("Notes", self.open_notes),
            ("System", self.open_system),
            (SEPARATOR, None),
            ("Preferences", self.open_preferences),
            (SEPARATOR, None),
            ("About", self.show_about),
        ])

    def open_preferences(self):
        """Open the searchable, full-screen configuration editor."""
        config.ensure_default_config()
        if self.mode != PREFERENCES:
            self._preferences_return = self.mode
            if self.mode == NETWORK:
                self._preferences_return_pane = self._network_pane_direction()
        self.switch_mode(PREFERENCES)

    def close_preferences(self):
        self.switch_mode(self._preferences_return)
        if self._preferences_return == NETWORK:
            self.focus_network_pane(self._preferences_return_pane)

    def _edit_preference(self, section, name, value, reopen, blank_resets=False,
                         modified=False, blank_unbinds=False):
        suffix = (" (blank = default)" if blank_resets else
                  " (blank = unbind)" if blank_unbinds else "")
        title = name + suffix

        def apply(value):
            try:
                config.save_preference(section, name, value)
                self._config_mtime = self._read_config_mtime()
                self.reload_config()
            except (OSError, ValueError) as exc:
                self.set_message(f"Preference not saved: {exc}")
            reopen()

        def save(text):
            apply(None if blank_resets and not text.strip() else text)

        initial = "" if blank_unbinds and value == "(unbound)" else value
        self.open_input_dialog(
            title, initial, len(initial), save,
            extra_label="Reset Default" if modified else None,
            on_extra=(lambda: apply(None)) if modified else None)

    def edit_preferences_file(self):
        """Open nshrc in an external editor for advanced manual changes."""
        config.ensure_default_config()
        self.edit_file(config.config_path())

    def show_about(self):
        lines = [
            "",
            f"nsh {__version__}",
            "",
            "https://github.com/naranicca/nsh",
        ]
        self._capture_dialog_focus()
        self.about_dialog.open("About", lines)
        self.application.layout.focus(self.about_dialog.control)
        self.invalidate()

    def show_error(self, title, lines):
        """Show an operation error in a modal instead of losing it in the status bar."""
        if isinstance(lines, str):
            lines = [lines]
        self._capture_dialog_focus()
        self.about_dialog.open(title, list(lines))
        self.application.layout.focus(self.about_dialog.control)
        self.invalidate()

    # -- remote connections --------------------------------------------------
    def open_network_menu(self):
        items = [
            ("Connect SFTP (SSH)", lambda: self._network_target("sftp")),
            ("Connect FTP", lambda: self._network_target("ftp")),
        ]
        if self.networkview.connected:
            items += [
                (SEPARATOR, None),
                ("Open current connection", lambda: self.switch_mode(NETWORK)),
                ("Disconnect", self.networkview.disconnect),
            ]
        self.open_menu("Network", items)

    def leave_network_views(self):
        """Drop every tab out of the remote views once the connection is gone
        The local remote split was shared by all tabs, so each one goes back to
        its own explorer layout - the right half returning to the preview."""
        for session in self.shells.sessions:
            if session.mode in (NETWORK, REMOTE_SHELL):
                session.mode = EXPLORER
    def open_remote_shell(self):
        backend = self.networkview.backend
        if backend is None or not hasattr(backend, "execute"):
            self.set_message("remote shell requires an SSH/SFTP connection")
            return
        if self.networkview.busy:
            self.set_message("wait for the remote operation to finish")
            return
        self.switch_mode(REMOTE_SHELL)

    def _network_target(self, protocol):
        example = (state.get("network_sftp_target", "user@host:22/")
                   if protocol == "sftp" else "user@host:21/")
        if not isinstance(example, str) or not example:
            example = ("user@host:22/" if protocol == "sftp"
                       else "user@host:21/")
        self.open_input_dialog(
            f"{protocol.upper()} target", example, len(example),
            lambda target: (self._network_sftp_route(target)
                            if protocol == "sftp"
                            else self._network_password(protocol, target)))

    def _network_sftp_route(self, target):
        if remote.has_configured_proxy(target):
            self._network_password("sftp", target)
        else:
            self._network_jump(target)

    def _network_jump(self, target):
        self.open_input_dialog(
            "Jump host (blank uses ~/.ssh/config)", "", 0,
            lambda jump: self._network_password("sftp", target, jump.strip()))

    def _network_password(self, protocol, target, jump=None):
        self.open_input_dialog(
            "Password (blank uses SSH key/anonymous)", "", 0,
            lambda password: self.networkview.connect(
                protocol, target, password, jump=jump),
            password=True)

    # -- input dialog ---------------------------------------------------------
    def open_input_dialog(self, title, text, cursor, on_accept,
                          on_change=None, on_cancel=None, password=False,
                          extra_label=None, on_extra=None):
        self._capture_dialog_focus()
        self.dialog.open(title, text, cursor, on_accept, on_change, on_cancel,
                         password=password, extra_label=extra_label,
                         on_extra=on_extra)
        self.application.layout.focus(self.dialog.control)
        self.invalidate()

    def _dialog_closed(self):
        control, self._dialog_return_focus = self._dialog_return_focus, None
        try:
            if control is None:
                raise ValueError("no dialog return focus")
            self.application.layout.focus(control)
        except Exception:  # noqa: BLE001 - stale/hidden control: mode fallback
            self._restore_focus()
        self.invalidate()

    def _capture_dialog_focus(self):
        try:
            self._dialog_return_focus = self.application.layout.current_control
        except Exception:
            self._dialog_return_focus = None

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
        # the message stays put until something explicitly clears it: a directory
        # change, a mode change, or ESC (no auto-dismiss, no slide-out animation)
        self.message = _safe_status_message(message)
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
        self.networkview.close()
        try:
            self.application.exit()
        except Exception:
            pass

    def _save_tab_state(self):
        """Persist normal explorer sessions, never a one-shot search picker."""
        if self.picker:
            return
        state.set("explorer_tabs", self.shells.snapshot()
                  if self._restore_tabs else None)

    async def _watch_cwd(self):
        """Poll the current directory and auto-refresh when it changes."""
        while True:
            try:
                await asyncio.sleep(1.0)
                if self.mode == GIT:
                    await self.refresh_git()  # reflect external edits in the list/diff
                else:
                    # poll what is on screen - while connected that is the
                    # network view's local pane, not the tab's active_pane
                    await asyncio.gather(*(pane.check_external_change()
                                           for pane in self._visible_panes()))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let the watcher die
                pass

    async def run_async(self):
        for ex in self.explorers:  # both panes start at the initial directory
            ex.load()
        self.shells.current()._needs_initial_load = False
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
            self._save_tab_state()
        return self.search_result
