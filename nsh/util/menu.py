"""A reusable popup menu: a vertical list of labelled actions.

Open it with :meth:`Menu.open` (a title plus ``(label, callback)`` pairs); the
arrow keys move the selection, Enter invokes it, Esc/Tab cancel. The widget owns
its focus/key handling; the host wires ``on_close`` to restore focus.
"""
from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import ConditionalContainer, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from .width import text_width


def _pad(s, width):
    return s + " " * max(0, width - text_width(s))


class Menu:
    def __init__(self, on_close):
        self._on_close = on_close
        self.active = False
        self.title = ""
        self.items = []  # [(label, callback)]
        self.cursor = 0
        self.scroll = 0

        self.control = FormattedTextControl(
            self._text,
            focusable=True,
            show_cursor=False,
            key_bindings=self._build_kb(),
        )
        self.container = ConditionalContainer(
            Window(self.control, dont_extend_width=True, dont_extend_height=True,
                   style="class:menu"),
            filter=Condition(lambda: self.active),
        )

    # -- lifecycle ------------------------------------------------------------
    def open(self, title, items):
        self.title = title
        self.items = list(items)
        self.cursor = 0
        self.scroll = 0
        self.active = True

    def close(self):
        self.active = False
        self._on_close()

    def _invoke(self):
        item = self.items[self.cursor] if self.items else None
        self.close()
        if item and item[1]:
            item[1]()

    def _move(self, delta):
        if self.items:
            self.cursor = max(0, min(len(self.items) - 1, self.cursor + delta))

    # -- rendering ------------------------------------------------------------
    def _visible_rows(self):
        """How many item rows fit: terminal height minus the chrome around us."""
        try:
            rows = get_app().output.get_size().rows
        except Exception:
            rows = 24
        # top float offset (2) + title (1) + status bar (1) + a margin (1)
        return max(1, rows - 5)

    def _text(self):
        labels = [lbl for lbl, _ in self.items]
        total = len(labels)
        vis = self._visible_rows()

        # keep the cursor within the visible window
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + vis:
            self.scroll = self.cursor - vis + 1
        self.scroll = max(0, min(self.scroll, max(0, total - vis)))

        width = max([text_width(self.title) + 8] + [text_width(l) + 2 for l in labels] + [12])
        title = self.title + (f"  ({self.cursor + 1}/{total})" if total > vis else "")
        out = [("class:menu.title", " " + _pad(title, width) + " \n")]

        shown = labels[self.scroll:self.scroll + vis]
        for j, label in enumerate(shown):
            i = self.scroll + j
            on = i == self.cursor
            style = "class:menu.selected" if on else "class:menu.item"
            if on:
                mark = "› "
            elif j == 0 and self.scroll > 0:
                mark = "▲ "
            elif j == len(shown) - 1 and self.scroll + vis < total:
                mark = "▼ "
            else:
                mark = "  "
            out.append((style, " " + _pad(mark + label, width) + " "))
            if j != len(shown) - 1:
                out.append(("", "\n"))
        return out

    # -- keys -----------------------------------------------------------------
    def _build_kb(self):
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("k")
        def _(event):
            self._move(-1)

        @kb.add("down")
        @kb.add("j")
        def _(event):
            self._move(1)

        @kb.add("enter")
        def _(event):
            self._invoke()

        @kb.add("escape")
        @kb.add("q")
        @kb.add("tab")
        def _(event):
            self.close()

        return kb
