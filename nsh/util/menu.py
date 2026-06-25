"""A reusable popup menu: a vertical list of labelled actions.

Open it with :meth:`Menu.open` (a title plus ``(label, callback)`` pairs); the
arrow keys move the selection, Enter invokes it, Esc/Tab cancel. The widget owns
its focus/key handling; the host wires ``on_close`` to restore focus.
"""
from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import ConditionalContainer, Window

from . import hangul
from .widgets import WheelScrollControl
from .width import text_width

# A divider row: pass ``(SEPARATOR, None)`` as an item to draw a horizontal line
# the cursor skips over. Use it to group related entries in a menu.
SEPARATOR = object()


def _is_sep(item):
    return item[0] is SEPARATOR


def _pad(s, width):
    return s + " " * max(0, width - text_width(s))


class Menu:
    def __init__(self, on_close):
        self._on_close = on_close
        self._extra_close = None  # optional one-shot close callback for this open
        self.active = False
        self.title = ""
        self.items = []  # [(label, callback)]
        self.cursor = 0
        self.scroll = 0

        self.control = WheelScrollControl(
            lambda d: self._move(d),  # wheel moves the selection
            on_click=self._on_mouse,  # click a row to invoke it
            text=self._text,
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
    def open(self, title, items, on_close=None):
        self.title = title
        self.items = list(items)
        # start on the first selectable row (skip a leading separator)
        self.cursor = next((i for i, it in enumerate(self.items)
                            if not _is_sep(it)), 0)
        self.scroll = 0
        self._extra_close = on_close
        self.active = True

    def close(self):
        self.active = False
        self._on_close()
        cb, self._extra_close = self._extra_close, None
        if cb:
            cb()

    def _invoke(self):
        item = self.items[self.cursor] if self.items else None
        if item is None or _is_sep(item):
            return  # the cursor never rests on a separator, but guard anyway
        self.close()
        if item and item[1]:
            item[1]()

    def _move(self, delta):
        """Move the selection by ``delta`` rows, stepping over separators (which
        are never selectable) and stopping at the ends."""
        if not self.items:
            return
        step = 1 if delta >= 0 else -1
        i = self.cursor
        for _ in range(abs(delta) or 1):
            j = i + step
            while 0 <= j < len(self.items) and _is_sep(self.items[j]):
                j += step
            if not (0 <= j < len(self.items)):
                break  # no selectable row left in this direction
            i = j
        self.cursor = i

    def _on_mouse(self, mouse_event):
        """Click a menu row to invoke it directly. Row 0 is the title; items
        start at row 1, offset by the current scroll."""
        y = mouse_event.position.y
        if y < 1:
            return
        i = self.scroll + (y - 1)
        if 0 <= i < len(self.items) and not _is_sep(self.items[i]):
            self.cursor = i
            self._invoke()

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
        total = len(self.items)
        vis = self._visible_rows()

        # keep the cursor within the visible window
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + vis:
            self.scroll = self.cursor - vis + 1
        self.scroll = max(0, min(self.scroll, max(0, total - vis)))

        # the menu width ignores separators (their label is the sentinel, not text)
        labels = [lbl for lbl, _ in self.items if lbl is not SEPARATOR]
        width = max([text_width(self.title) + 8] + [text_width(l) + 2 for l in labels] + [12])
        title = self.title + (f"  ({self.cursor + 1}/{total})" if total > vis else "")
        out = [("class:menu.title", " " + _pad(title, width) + " \n")]

        shown = self.items[self.scroll:self.scroll + vis]
        for j, item in enumerate(shown):
            i = self.scroll + j
            if _is_sep(item):
                out.append(("class:menu.separator", " " + "─" * width + " "))
                if j != len(shown) - 1:
                    out.append(("", "\n"))
                continue
            label = item[0]
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
        @kb.add("tab")
        def _(event):
            self._invoke()

        @kb.add("escape")
        @kb.add("q")
        def _(event):
            self.close()

        hangul.add_hangul_aliases(kb)  # j/k/q work with the Korean IME on too
        return kb
