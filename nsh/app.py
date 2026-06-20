"""The nsh application: layout, the two modes, and central dispatch."""
import asyncio
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import CompleteEvent
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

from . import __version__, config
from .explorer import git
from .explorer.gitview import GitView
from .explorer.logview import LogView
from .explorer.preview import PreviewView
from .explorer.view import ExplorerView
from .notes.view import NotesView
from .search.view import SearchView
from .system.view import SystemView
from .shell.runner import CommandRunner
from .shell.tabs import ShellTabs
from .util.bookmarks import Bookmarks
from .util.dialog import ConfirmDialog, FindTextDialog, InfoDialog, InputDialog
from .util.menu import Menu
from .util.paths import shorten_home
from .util.width import char_width, cut_to_width, text_width

EXPLORER = "explorer"
SHELL = "shell"
SEARCH = "search"
GIT = "git"
LOG = "gitlog"
NOTES = "notes"
SYSTEM = "system"

# Once the shell output would shrink the explorer below this many rows, the
# shell takes over the whole screen.
SHELL_MIN_EXPLORER = 5


def _unquote_arg(s):
    """Strip a surrounding quote pair from a single shell argument.

    Tab-completion wraps a name with a space in double quotes (a directory keeps
    its quote open, e.g. ``"New folder/``); ``cd`` gets the raw line, so peel the
    leading quote and a matching trailing one before treating it as a path.
    """
    s = s.strip()
    if s[:1] in ("'", '"'):
        q = s[0]
        s = s[1:]
        if s.endswith(q):
            s = s[:-1]
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
        self.mode = EXPLORER
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
        # two side-by-side explorer panes (Norton/MC-style). In single-pane mode
        # only the active pane is shown; in two-pane mode both are, and F7/F8
        # switch which one is active (and holds the cursor). Each pane keeps its
        # own directory, selection and cursor.
        self.explorers = [ExplorerView(self, initial_cwd),
                          ExplorerView(self, initial_cwd)]
        self.active_pane = 0
        self.two_pane = (self.settings.get("two_pane", "false").strip().lower()
                         in ("true", "1", "yes", "on"))
        self.shells = ShellTabs(self)  # the shell sessions, managed as tabs
        self.preview = PreviewView(self)
        self.show_preview = True
        self.search = SearchView(self)
        self.gitview = GitView(self)
        self.logview = LogView(self)
        self.notesview = NotesView(self)
        self.systemview = SystemView(self)
        self._log_return = EXPLORER  # the mode "Git: Log" was opened from

        # popup action menu (Tab in the explorer)
        self.menu = Menu(self._menu_closed)
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

        self.application = self._build_application()

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
    def shell(self):
        """The active shell session (most code only ever touches this one)."""
        return self.shells.current()

    def focus_shell(self):
        self.application.layout.focus(self.shells.current().command_buffer)

    def _prefill_selection(self):
        """Entering the shell from the explorer with files selected drops their
        names into the (empty) prompt, ready to use as command arguments."""
        sel = self.explorer.selected
        if not sel:
            return
        buff = self.shells.current().command_buffer
        if buff.text:
            return  # don't clobber a half-typed command
        # listing order, then any selected paths not currently listed; each is
        # relative to the cwd and quoted when it contains a space
        ordered = [e.path for e in self.explorer.entries if e.path in sel]
        listed = set(ordered)
        parts = []
        for p in ordered + [q for q in sel if q not in listed]:
            rel = os.path.relpath(str(p), str(self.cwd)).replace(os.sep, "/")
            parts.append(f'"{rel}"' if " " in rel else rel)
        if parts:
            buff.text = " ".join(parts) + " "
            buff.cursor_position = len(buff.text)

    # -- layout ---------------------------------------------------------------
    def _build_application(self):
        confirm_open = Condition(lambda: self.confirm_dialog.active)
        menu_open = Condition(lambda: self.menu.active)
        dialog_open = Condition(lambda: self.dialog.active)
        about_open = Condition(lambda: self.about_dialog.active)
        find_open = Condition(lambda: self.find_dialog.active)
        overlay_open = confirm_open | menu_open | dialog_open | about_open | find_open

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

        # Ctrl+N: Notes (a scratch pad of multi-line notes). Explorer/git only.
        @kb.add("c-n", filter=~overlay_open & find_modes)
        def _(event):
            self.open_notes()

        @kb.add("escape", filter=~overlay_open)
        def _(event):
            buff = self.shell.command_buffer
            self.message = ""  # ESC dismisses the status message
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
            elif self.mode in (NOTES, SYSTEM):
                self.switch_mode(EXPLORER)
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

        # In two-pane explorer mode, F7/F8 move the cursor between the two panes
        # (mirroring the shell's F7/F8 tab switch). The previous pane keeps its
        # directory/selection but loses the cursor highlight.
        two_pane_active = Condition(lambda: self.mode == EXPLORER and self.two_pane)

        @kb.add("f7", filter=two_pane_active)
        @kb.add("f8", filter=two_pane_active)
        def _(event):
            self.switch_pane()

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
                self.explorers[0].window,
                Window(width=1, char="│", style="class:preview.border"),
                self.preview.window,
            ]
        )

        # two-pane view: the two explorer panes side by side, no preview
        self._two_pane_split = VSplit(
            [
                self.explorers[0].window,
                Window(width=1, char="│", style="class:preview.border"),
                self.explorers[1].window,
            ]
        )

        # the explorer area, reused both in explorer mode and on top of the
        # shell so the listing stays visible. Two-pane shows both panes (no
        # preview); single-pane shows pane 0 with the optional preview beside it.
        explorer_area = DynamicContainer(
            lambda: self._two_pane_split if self.two_pane
            else (self._explorer_split
                  if (self.show_preview and self._wide_enough())
                  else self.explorers[0].window)
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
            if self.mode == NOTES:
                return self.notesview.container
            if self.mode == SYSTEM:
                return self.systemview.container
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
                Float(content=self.about_dialog.container),
                Float(content=self.find_dialog.container),
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

    def _pane_git_segs(self, i):
        """Branch (+ in-progress merge/rebase) and selected-count badge for pane
        ``i``, from that pane's own git status / selection — so each pane in the
        two-pane title shows its own branch."""
        segs = []
        gs = self.explorers[i].git_status
        branch = self._branch_seg(gs)
        if branch:
            segs.append(("class:titlebar", " on "))
            segs.append(branch)
            if gs.in_progress:
                segs.append(("class:titlebar", " "))
                segs.append(("class:titlebar.branch.dirty", f"⚠ {gs.in_progress}"))
        sel = self.explorers[i].selected
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

    def _title_text(self):
        # the "nsh" label adopts the menu's header colour while a menu is open,
        # so it's obvious a popup is active; otherwise it blends into the bar.
        name_style = "class:menu.title" if self.menu.active else "class:titlebar.name"
        clock = [("class:titlebar.clock", f" {datetime.now().strftime('%H:%M:%S')} ")]
        if self.two_pane:
            return self._two_pane_title(name_style, clock)
        segs = [
            (name_style, " nsh "),
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
        if self.mode == GIT:
            segs.append(("class:titlebar", "   "))
            segs.append(("class:titlebar.branch", "● git"))
        if self.mode == LOG:
            segs.append(("class:titlebar", "   "))
            segs.append(("class:titlebar.branch", "● log"))
        if self.mode == NOTES:
            segs.append(("class:titlebar", "   "))
            segs.append(("class:titlebar.branch", "● notes"))
        if self.mode == SYSTEM:
            segs.append(("class:titlebar", "   "))
            segs.append(("class:titlebar.branch", "● system"))
        selected = self.active_selection()
        if selected:
            segs.append(("class:titlebar", "   "))
            segs.append(("class:titlebar.sel", f"● {len(selected)} selected"))
        return self._fill_with_right(segs, "class:titlebar", clock)

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
            active = i == self.active_pane
            style = "class:titlebar.path" if active else "class:titlebar"
            marker = "▸ " if active else "  "
            git_segs = self._pane_git_segs(i)
            git_w = sum(text_width(t) for _, t in git_segs)
            path = self._clip_path(
                shorten_home(self.explorers[i].cwd),
                max(0, avail - text_width(marker) - git_w))
            return [(style, marker + path)] + git_segs

        # left half: nsh label + left pane path + its branch/selected
        label = [(name_style, " nsh "), ("class:titlebar", " ")]
        label_w = sum(text_width(t) for _, t in label)
        left = label + pane_region(0, lw - label_w)
        left = self._clip_segs(left, lw, "class:titlebar")
        # right half: right pane path + its branch/selected, clipped so it stops
        # before the clock, then padded so the clock lands at the edge
        right = pane_region(1, rw - clock_w)
        right = self._clip_segs(right, max(0, rw - clock_w), "class:titlebar")
        return left + [("class:titlebar", " " * sep)] + right + clock

    def _status_text(self):
        if self.mode == EXPLORER:
            hints = [
                ("↵", "open"), ("Space", "select"),
                ("Tab", "actions"), ("b", "marks"), ("/", "find"), ("*", "select"),
                ("^N", "note"), (":", "cmd"),
            ]
            # the 2-pane toggle; once in it, surface the F7/F8 pane switch instead
            if self.two_pane:
                hints.append(("F7/8", "pane"))
                hints.append(("2", "1-pane"))
            else:
                hints.append(("2", "2-pane"))
            hints.append(("q", "quit"))
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
        elif self.mode == NOTES:
            hints = [
                ("^S", "save"), ("↑↓", "browse"), ("/", "search"), ("↵", "edit"),
                ("d/x", "delete"), ("u", "undo"), ("ESC", "back"),
            ]
        elif self.mode == SYSTEM:
            hints = [
                ("↑↓", "move"), ("c/m/n", "sort cpu/mem/name"),
                ("/", "search"), ("x", "kill"), ("K", "force"), ("r", "refresh"),
                ("ESC", "back"),
            ]
        else:
            hints = [
                ("Tab", "complete"), ("↵", "run"), ("↑↓", "history"),
                ("PgUp/PgDn", "scroll"), ("^T/^W", "tab"), ("Alt+←→/F7·8", "switch"),
                ("^C", "stop"), ("ESC", "explorer"),
            ]
        segs = []
        # a yellow square at the very front whenever there are saved notes —
        # ahead of the message and the shortcut hints (Ctrl+N to view them)
        if len(self.notesview.notes) > 0:
            segs.append(("class:statusbar.notes", " ■ "))
        # the message sits in front of the shortcuts and stays until it's
        # explicitly cleared (directory change, mode change, or ESC)
        if self.message:
            segs.append(("class:statusbar.msg", f" {self.message} "))
        for key, label in hints:
            segs.append(("class:statusbar.key", f" {key} "))
            segs.append(("class:statusbar", f"{label} "))
        return self._fill(segs, "class:statusbar")

    # -- modes ----------------------------------------------------------------
    def toggle_mode(self):
        self.switch_mode(EXPLORER if self.mode == SHELL else SHELL)

    def switch_mode(self, mode):
        # remember where the shell was opened from, to return there on ESC
        from_mode = self.mode
        if mode == SHELL and self.mode in (EXPLORER, GIT):
            self._shell_return = self.mode
        if from_mode != mode:
            self.message = ""  # a mode change dismisses the status message
        self.mode = mode
        if mode == SHELL:
            if from_mode == EXPLORER:
                self._prefill_selection()
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

    # -- find (text / file) ---------------------------------------------------
    def open_find(self):
        """Find: choose between searching file *contents* (grep) or file *names*
        (the fuzzy finder)."""
        self.open_menu("Find", [
            ("Text (grep)", self.find_text),
            ("File (fuzzy)", lambda: self.enter_search()),
        ])

    def find_text(self):
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
        self.run_in_shell(self.shell, cmd)

    # -- notes ----------------------------------------------------------------
    def open_notes(self):
        self.switch_mode(NOTES)

    # -- system (process manager) ---------------------------------------------
    def open_system(self):
        self.switch_mode(SYSTEM)

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
        self.explorer.cwd = target  # the active pane owns the directory
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

    def _git_panes(self):
        """The panes whose git status should be kept fresh: both when two-pane
        view is on (so the inactive pane's markers show too), else just active."""
        return list(self.explorers) if self.two_pane else [self.explorer]

    def schedule_git(self):
        if self._git_task and not self._git_task.done():
            self._git_task.cancel()
        # reset the active pane while its query runs; the other pane keeps its
        # current markers until its own query returns (no flicker on nav)
        self.explorer.git_status = git.GitStatus()
        targets = [(ex, ex.cwd) for ex in self._git_panes()]
        self._git_task = asyncio.ensure_future(self._git_worker(targets))

    async def _git_worker(self, targets):
        for ex, path in targets:
            status = await git.query(path)
            if path == ex.cwd:  # ignore results for a directory the pane left
                ex.git_status = status
        self.gitview.on_status_changed()
        self.invalidate()

    async def refresh_git(self):
        # refresh every visible pane (a two-pane copy/move changes the other
        # pane's directory too)
        for ex in self._git_panes():
            ex.git_status = await git.query(ex.cwd)
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
        else:
            self.application.layout.focus(self.explorer.control)

    # -- confirmation dialog --------------------------------------------------
    def confirm(self, label, callback):
        """Show a centered yes/no dialog; ``callback(True|False)`` on resolve."""
        self.confirm_dialog.open("Confirm", label, callback)
        self.application.layout.focus(self.confirm_dialog.control)
        self.invalidate()

    # -- action menu ----------------------------------------------------------
    def open_menu(self, title, items, on_close=None):
        self.menu.open(title, items, on_close)
        self.application.layout.focus(self.menu.control)
        self.invalidate()

    def _menu_closed(self):
        self._restore_focus()
        self.invalidate()

    # -- two-pane view --------------------------------------------------------
    def switch_pane(self):
        """Move the cursor to the other explorer pane (two-pane mode). The cwd,
        git status, title bar and shell all follow the newly active pane."""
        if not self.two_pane:
            return
        self.active_pane ^= 1  # two panes: toggle
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

    def toggle_two_pane(self):
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
        self.open_menu("nsh", [
            ("Find", self.open_find),
            ("Notes", self.open_notes),
            ("System", self.open_system),
            ("Preferences", self.open_preferences),
            ("About", self.show_about),
        ])

    def open_preferences(self):
        """Open the nshrc config file in the editor (seeding it first if absent).
        Edits take effect the next time nsh starts."""
        config.ensure_default_config()
        self.edit_file(config.config_path())

    def show_about(self):
        lines = [
            "",
            f"nsh {__version__}",
            "",
            "https://github.com/naranicca/nsh",
        ]
        self.about_dialog.open("About", lines)
        self.application.layout.focus(self.about_dialog.control)
        self.invalidate()

    # -- input dialog ---------------------------------------------------------
    def open_input_dialog(self, title, text, cursor, on_accept,
                          on_change=None, on_cancel=None):
        self.dialog.open(title, text, cursor, on_accept, on_change, on_cancel)
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
        # the message stays put until something explicitly clears it: a directory
        # change, a mode change, or ESC (no auto-dismiss, no slide-out animation)
        self.message = message
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
                    if self.two_pane:  # keep the inactive pane fresh too
                        self.explorers[1 - self.active_pane].check_external_change()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let the watcher die
                pass

    async def run_async(self):
        for ex in self.explorers:  # both panes start at the initial directory
            ex.load()
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
