"""Manage several :class:`ShellView` sessions as tabs.

Only one session is visible at a time (its output + input line); a thin tab bar
below the prompt lists the others; a tab whose command is still running is
tinted orange. A new session is spawned automatically when a command is entered while
the active session is busy, or explicitly with Ctrl-T.
"""
from prompt_toolkit.layout.containers import (
    DynamicContainer,
    HSplit,
    Window,
)

from ..util.widgets import WheelScrollControl
from ..util.width import cut_to_width, text_width
from .view import ShellView

MAX_TAB_LABEL = 14


class ShellTabs:
    def __init__(self, app):
        self.app = app
        self.sessions = [ShellView(app)]
        self.active = 0
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

    # -- tab operations -------------------------------------------------------
    def new_session(self) -> ShellView:
        """Create a tab, make it active, and return it."""
        session = ShellView(self.app)
        self.sessions.append(session)
        self.active = len(self.sessions) - 1
        self.app.focus_shell()
        return session

    def select(self, idx):
        if 0 <= idx < len(self.sessions):
            self.active = idx
            self.app.focus_shell()
            self.app.invalidate()

    def next(self):
        self.select((self.active + 1) % len(self.sessions))

    def prev(self):
        self.select((self.active - 1) % len(self.sessions))

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
        self.app.focus_shell()
        self.app.invalidate()

    # -- rendering ------------------------------------------------------------
    def _on_mouse(self, mouse_event):
        """Switch to the tab under the click (x maps through the spans recorded
        while rendering the bar). With a menu open the click dismisses it."""
        if self.app.consume_menu_click():
            return
        x = mouse_event.position.x
        for start, end, idx in self._tab_spans:
            if start <= x < end:
                self.select(idx)
                return

    def _on_overview_mouse(self, mouse_event):
        """Click on the out-of-shell-mode bar: jump into that shell tab."""
        if self.app.consume_menu_click():
            return
        x = mouse_event.position.x
        for start, end, idx in self._tab_spans:
            if start <= x < end:
                self.app.open_shell_tab(idx)
                return

    def _tabbar_text(self):
        frags = []
        spans = []
        col = 0
        for i, s in enumerate(self.sessions):
            active = i == self.active
            busy = s.busy()
            # a still-running tab goes orange (the active variant fills the tab so
            # the blue highlight doesn't fight the orange)
            if active:
                base = "class:shell.tab.active.busy" if busy else "class:shell.tab.active"
            else:
                base = "class:shell.tab.busy" if busy else "class:shell.tab"
            label = cut_to_width(s.custom_title or s.title or "shell", MAX_TAB_LABEL)
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
        self._tab_spans = spans
        return frags
