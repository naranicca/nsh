"""Manage several :class:`ShellView` sessions as tabs.

Only one session is visible at a time (its output + input line); a thin tab bar
on top lists the others and marks which ones still have a command running. A new
session is spawned automatically when a command is entered while the active
session is busy, or explicitly with Ctrl-T.
"""
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    DynamicContainer,
    HSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl

from ..util.width import cut_to_width
from .view import ShellView

MAX_TAB_LABEL = 14


class ShellTabs:
    def __init__(self, app):
        self.app = app
        self.sessions = [ShellView(app)]
        self.active = 0

        self._tabbar = Window(
            FormattedTextControl(self._tabbar_text),
            height=1,
            style="class:shell.tabbar",
        )
        self.container = HSplit([
            # the bar only appears once there is more than one tab
            ConditionalContainer(
                self._tabbar, filter=Condition(lambda: len(self.sessions) > 1)
            ),
            DynamicContainer(lambda: self.current().container),
        ])

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
    def _tabbar_text(self):
        frags = []
        for i, s in enumerate(self.sessions):
            active = i == self.active
            base = "class:shell.tab.active" if active else "class:shell.tab"
            label = cut_to_width(s.title or "shell", MAX_TAB_LABEL)
            frags.append((base, f" {i + 1}:{label} "))
            # running indicator: a green dot, or a blank to keep the width steady.
            # Layer the dot's green fg (shell.running carries no background) over
            # the tab's own class so it keeps the tab background — the active
            # tab's highlight, not a black block.
            frags.append((f"{base} class:shell.running" if s.busy() else base,
                          "●" if s.busy() else " "))
            frags.append((base, " "))
            frags.append(("class:shell.tabbar", " "))
        return frags
