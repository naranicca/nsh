"""Manage several tabs, each its own explorer + shell session.

A tab bundles a :class:`ShellView` (its scrollback, input line and running
process) with its own explorer pane(s) — so switching tabs swaps the whole
working context, not just the command line. Only one tab is visible at a time;
a thin tab bar below the prompt lists the others; a tab whose command is still
running is tinted orange. Commands entered while it is busy are queued in that
tab; a new tab can be opened explicitly with Ctrl-T.
"""
from pathlib import Path

from prompt_toolkit.layout.containers import (
    DynamicContainer,
    HSplit,
    Window,
)

from ..explorer.gitview import GitView
from ..explorer.logview import LogView
from ..explorer.view import ExplorerView
from ..util.widgets import WheelScrollControl
from ..util.width import cut_to_width, text_width
from .view import ShellView

MAX_TAB_LABEL = 14
NEW_TAB = "+"  # sentinel span id for the "+ new tab" button (vs integer tab ids)
MAX_RESTORED_TABS = 50
MAX_RESTORED_COMMANDS = 500


def restored_tab_specs(snapshot):
    """Validate persisted tab data and return safe, normalized tab specs."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("tabs"), list):
        return [], 0
    try:
        saved_active = int(snapshot.get("active", 0))
    except (TypeError, ValueError):
        saved_active = 0
    specs = []
    active = 0
    for saved_index, saved in enumerate(snapshot["tabs"][:MAX_RESTORED_TABS]):
        if not isinstance(saved, dict):
            continue
        raw_paths = saved.get("paths")
        if not isinstance(raw_paths, list):
            continue
        paths = []
        for raw in raw_paths[:2]:
            try:
                path = Path(raw).expanduser()
                if path.is_dir():
                    paths.append(path)
            except (OSError, TypeError, ValueError):
                pass
        if not paths:
            continue
        if len(paths) == 1:
            paths.append(paths[0])
        title = saved.get("title")
        raw_history = saved.get("history", [])
        history = ([item for item in raw_history if isinstance(item, str)
                    and item.strip()][-MAX_RESTORED_COMMANDS:]
                   if isinstance(raw_history, list) else [])
        specs.append({
            "paths": paths,
            "active_pane": 1 if saved.get("active_pane") == 1 else 0,
            "two_pane": bool(saved.get("two_pane", False)),
            "title": title if isinstance(title, str) and title.strip() else None,
            "history": history,
        })
        if saved_index <= saved_active:
            active = len(specs) - 1
    return specs, active


class ShellTabs:
    def __init__(self, app, initial_cwd, snapshot=None):
        self.app = app
        # the first tab's explorer starts at the launch directory; later tabs
        # open at whatever directory was current when they were created
        specs, active = restored_tab_specs(snapshot)
        self.sessions = []
        for spec in specs:
            session = self._new_tab(spec["paths"][0])
            for explorer, path in zip(session.explorers, spec["paths"]):
                explorer.cwd = path
            session.active_pane = spec["active_pane"]
            session.two_pane = spec["two_pane"]
            session.custom_title = spec["title"]
            for command in spec["history"]:
                session.command_buffer.history.append_string(command)
            session._needs_initial_load = True
            self.sessions.append(session)
        if not self.sessions:
            self.sessions = [self._new_tab(initial_cwd)]
        self.active = active
        self._tab_spans = []  # [(start_col, end_col, idx)] for click hit-testing

        self._tabbar = Window(
            WheelScrollControl(
                lambda d: self.next() if d > 0 else self.prev(),  # wheel cycles
                on_click=self._on_mouse,  # click a tab to switch to it
                text=self._tabbar_text),
            height=1,
            style="class:shell.tabbar",
        )
        self.container = HSplit([
            DynamicContainer(lambda: self.current().container),
            self._tabbar,  # below the prompt; always shown, even with one tab
        ])
        # a second copy of the bar shown outside shell mode (above the status
        # bar) so open shells stay visible; clicking a tab jumps into it
        self.overview_bar = Window(
            WheelScrollControl(
                lambda d: self.next() if d > 0 else self.prev(),
                on_click=self._on_overview_mouse,
                text=self._tabbar_text),
            height=1,
            style="class:shell.tabbar",
        )

    def has_open_shell(self) -> bool:
        """True when there's a shell worth showing outside shell mode: more than
        one tab, a running command, or a session with scrollback."""
        if len(self.sessions) > 1:
            return True
        return any(s.busy() or s.lines for s in self.sessions)

    # -- access ---------------------------------------------------------------
    def current(self) -> ShellView:
        return self.sessions[self.active]

    def any_running(self) -> bool:
        return any(s.busy() for s in self.sessions)

    def interrupt_all(self):
        for s in self.sessions:
            s.runner.interrupt()

    @staticmethod
    def automatic_title(session) -> str:
        """Current directory name for a tab without a user title.

        Derive this at render time instead of storing it, because a tab can
        change directory through the shell, bookmarks, restored state, or by
        moving focus between its two explorer panes.
        """
        try:
            explorer = session.explorers[session.active_pane]
            cwd = Path(explorer.cwd)
        except (AttributeError, IndexError, TypeError, ValueError):
            return "shell"
        return cwd.name or cwd.anchor or str(cwd) or "shell"

    @classmethod
    def title_for(cls, session) -> str:
        return session.custom_title or cls.automatic_title(session)

    # -- tab operations -------------------------------------------------------
    def _new_tab(self, cwd, mode=None) -> ShellView:
        """Build a tab: a shell session carrying its own pair of explorer panes
        (single-pane shows pane 0; two-pane shows both). The explorers are
        attached to the session so switching tabs swaps them with the shell."""
        from ..app import EXPLORER  # local import avoids an app<->tabs cycle
        session = ShellView(self.app)
        session.explorers = [ExplorerView(self.app, cwd),
                             ExplorerView(self.app, cwd)]
        for ex in session.explorers:
            ex.session = session  # so its git output logs to this tab (not the active one)
        session.active_pane = 0
        session.two_pane = self.app._two_pane_default  # per-tab; seeded from nshrc
        # which half of the connected local|remote split this tab sits on
        # (-1 local, +1 remote). Per-tab, like the mode itself, so switching
        # tabs restores the cursor where it was instead of always jumping to
        # the one shared remote pane. A tab stars on its own local list
        session.network_pane = -1
        # each tab also owns its own git mode and git log views (their own list,
        # cursor and selection), so F7/F8 swap them with the explorer
        session.gitview = GitView(self.app)
        session.gitview.window.width = (
            lambda: self.app._pane_dim(not self.app.preview_focused()))
        session.logview = LogView(self.app)
        session.logview.window.width = (
            lambda: self.app._pane_dim(not self.app.preview_focused()))
        # the mode to return to when leaving the log view (per-tab, since the log
        # is now per-tab and tab-navigable)
        session.log_return = EXPLORER
        # the mode (explorer / shell / git / log) is per-tab too, so leaving git
        # or log mode in one tab doesn't pull the others out of it
        session.mode = mode if mode is not None else EXPLORER
        return session

    def new_session(self) -> ShellView:
        """Create a tab at the current directory, make it active, and return it.
        Its explorer panes are loaded and the app follows the new tab (cwd,
        preview, git status and focus). The new tab opens in the mode you're in
        now (explorer / shell / git) so Ctrl+T keeps your workflow. Opened from
        the Network view it starts as an explorer, which _after_tab_switch turns
        straight back into the local|remote split - the tab keeps two panes,
        with its own local half beside the shared remote one."""
        from ..app import EXPLORER, GIT, LOG, SHELL
        self.app.remember_network_pane()  # the tab being left keeps its half
        cur = self.app.mode
        mode = cur if cur in (EXPLORER, SHELL, GIT, LOG) else EXPLORER
        session = self._new_tab(self.app.cwd, mode)
        for ex in session.explorers:
            ex.load()
        self.sessions.append(session)
        self.active = len(self.sessions) - 1
        self.app._after_tab_switch()
        return session

    def snapshot(self):
        """Serializable explorer state used for next-launch restoration."""
        return {
            "version": 1,
            "active": self.active,
            "tabs": [{
                "paths": [str(ex.cwd) for ex in session.explorers],
                "active_pane": session.active_pane,
                "two_pane": session.two_pane,
                "title": session.custom_title,
                "history": session.command_buffer.history.get_strings()[
                    -MAX_RESTORED_COMMANDS:],
                "input": session.command_buffer.text[:MAX_RESTORED_INPUT],
            } for session in self.sessions],
        }

    def ensure_loaded(self, session=None):
        session = session or self.current()
        if not getattr(session, "_needs_initial_load", False):
            return
        for explorer in session.explorers:
            explorer.load()
        session._needs_initial_load = False

    def select(self, idx):
        if 0 <= idx < len(self.sessions):
            self.app.remember_network_pane()
            self.active = idx
            self.ensure_loaded()
            self.app._after_tab_switch()

    def next(self):
        self.select((self.active + 1) % len(self.sessions))

    def prev(self):
        self.select((self.active - 1) % len(self.sessions))

    def move(self, direction):
        """Reorder: swap the current tab with its neighbour and follow it."""
        target = self.active + direction
        if not 0 <= target < len(self.sessions):
            return False
        sessions = self.sessions
        sessions[self.active], sessions[target] = (
            sessions[target], sessions[self.active])
        self.active = target  # the index moved; the current session did not
        self.app.invalidate()
        return True

    def rename(self, session=None):
        """Prompt for a custom label for a tab (default: the active one)."""
        session = session or self.current()
        current = session.custom_title or ""

        def _apply(name):
            name = name.strip()
            # an empty name clears the override, reverting to the auto title
            session.custom_title = name or None
            self.app.invalidate()

        self.app.open_input_dialog("Rename tab", current, len(current), _apply)

    def request_close(self, session=None):
        """Close a tab, but if its command is still running, confirm first."""
        session = session or self.current()
        if session.busy():
            label = self.title_for(session)
            self.app.confirm(
                f"'{label}' is still running. Close the tab and stop it?",
                lambda ok: self.close(session) if ok else None)
        else:
            self.close(session)

    def close(self, session=None):
        """Close a tab (default: the active one); its process is killed.

        Closing the last remaining tab leaves shell mode — back to wherever the
        shell was opened from (explorer, or git mode) — keeping the now-empty
        session for next time rather than removing it.
        """
        session = session or self.current()
        session.runner.interrupt()
        if len(self.sessions) <= 1:
            session.clear()
            self.app.switch_mode(self.app._shell_return)
            return
        idx = self.sessions.index(session)
        self.sessions.pop(idx)
        self.active = min(self.active, len(self.sessions) - 1)
        self.app._after_tab_switch()  # the now-current tab's explorer takes over

    # -- rendering ------------------------------------------------------------
    def _on_mouse(self, mouse_event):
        """Switch to the tab under the click (x maps through the spans recorded
        while rendering the bar); a double-click closes it. With a menu open the
        click dismisses it."""
        if self.app.consume_menu_click():
            return
        x = mouse_event.position.x
        for start, end, idx in self._tab_spans:
            if start <= x < end:
                if idx == NEW_TAB:
                    self.new_session()
                elif self.app.double_click(("shelltab",), idx):
                    self.request_close(self.sessions[idx])
                else:
                    self.select(idx)
                return

    def _on_overview_mouse(self, mouse_event):
        """Click on the out-of-shell-mode bar: switch to that tab (swapping its
        explorer) while staying in the current mode; a double-click closes it.
        The "+" button opens a new tab."""
        if self.app.consume_menu_click():
            return
        x = mouse_event.position.x
        for start, end, idx in self._tab_spans:
            if start <= x < end:
                if idx == NEW_TAB:
                    self.new_session()
                elif self.app.double_click(("shelltab",), idx):
                    self.request_close(self.sessions[idx])
                else:
                    self.select(idx)
                return

    def _tabbar_text(self):
        frags = []
        spans = []
        col = 0
        for i, s in enumerate(self.sessions):
            active = i == self.active
            busy = s.busy()
            # a still-running tab goes orange; a finished one whose last command
            # failed goes red (busy wins, so a fresh command clears the red). The
            # active variant fills the tab so the highlight stays legible.
            err = not busy and s.errored()
            if active:
                if busy:
                    base = "class:shell.tab.active.busy"
                elif err:
                    base = "class:shell.tab.active.err"
                else:
                    base = "class:shell.tab.active"
            else:
                if busy:
                    base = "class:shell.tab.busy"
                elif err:
                    base = "class:shell.tab.err"
                else:
                    base = "class:shell.tab"
            label = cut_to_width(self.title_for(s), MAX_TAB_LABEL)
            main = f" {i + 1}:{label} "
            start = col
            frags.append((base, main))
            # a dot still marks a running command (a blank keeps the width steady
            # so labels don't shift when it finishes); it rides the tab's own
            # style, orange included.
            frags.append((base, "●" if busy else " "))
            frags.append((base, " "))
            frags.append(("class:shell.tabbar", " "))
            col += text_width(main) + 3  # dot + gap + tabbar separator
            spans.append((start, col, i))
        # a "+" button at the right end opens a new tab with the mouse
        plus = " + "
        start = col
        frags.append(("class:shell.tab.new", plus))
        col += text_width(plus)
        spans.append((start, col, NEW_TAB))
        self._tab_spans = spans
        return frags
