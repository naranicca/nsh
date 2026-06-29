"""Centered modal dialogs.

``InputDialog`` — a text field with OK / Cancel buttons.
``ConfirmDialog`` — a message with Yes / No buttons.
``InfoDialog`` — read-only text with an OK button.
``FindTextDialog`` — a phrase plus case-sensitive / whole-word toggles.
``ChmodDialog`` — a 3×3 rwx grid (owner/group/other) with a live octal readout.

All: arrow keys / Tab move between fields/buttons, Enter runs the primary action,
Esc dismisses.
"""
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    HSplit,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.widgets import Frame

from .width import char_width, cut_to_width, text_width

WIDTH = 50


def _on_click(action):
    """Wrap ``action`` (no args) as a fragment mouse handler firing on press."""
    def handler(mouse_event):
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            action()
        return None
    return handler


def _button(label, active, action=None):
    """A button fragment; with ``action`` it carries a click handler (a 3-tuple
    fragment), which prompt_toolkit routes through the centred row for us."""
    style = "class:dialog.button.focus" if active else "class:dialog.button"
    text = f"  {label}  "
    return (style, text, _on_click(action)) if action is not None else (style, text)


class InputDialog:
    def __init__(self, on_close):
        self._on_close = on_close
        self.active = False
        self.title = ""
        self.button = "ok"          # which button Enter triggers: "ok" | "cancel"
        self._on_accept = None
        self._on_change = None      # live callback fired on every text edit
        self._on_cancel = None      # called when the dialog is dismissed (Esc)
        self.buffer = Buffer(multiline=False, on_text_changed=self._text_changed)

        self.control = BufferControl(self.buffer, key_bindings=self._kb())
        body = HSplit(
            [
                Window(self.control, height=1, style="class:dialog.input",
                       width=Dimension.exact(WIDTH)),
                Window(height=1),  # spacer
                Window(FormattedTextControl(self._buttons), height=1,
                       align=WindowAlign.CENTER),
            ],
            style="class:dialog",
            padding=0,
        )
        self.container = ConditionalContainer(
            Frame(body, title=lambda: self.title),
            filter=Condition(lambda: self.active),
        )

    def open(self, title, text, cursor, on_accept, on_change=None, on_cancel=None):
        self.title = title
        self._on_accept = on_accept
        # set the live-edit hooks before seeding the text, so a non-empty initial
        # value (rare) reaches on_change too
        self._on_change = on_change
        self._on_cancel = on_cancel
        self.button = "ok"
        self.active = True
        self.buffer.text = text
        self.buffer.cursor_position = max(0, min(cursor, len(text)))

    def _text_changed(self, _buffer):
        if self._on_change:
            self._on_change(self.buffer.text)

    def cancel(self):
        on_cancel = self._on_cancel
        self._on_accept = None
        self._close()
        if on_cancel:
            on_cancel()

    def _accept(self):
        if self.button == "cancel":
            self.cancel()
            return
        text = self.buffer.text
        callback = self._on_accept
        self._close()
        if callback:
            callback(text)

    def _close(self):
        self.active = False
        self._on_accept = None
        self._on_change = None
        self._on_cancel = None
        self._on_close()

    def _toggle(self):
        self.button = "cancel" if self.button == "ok" else "ok"

    def _click_ok(self):
        self.button = "ok"
        self._accept()

    def _buttons(self):
        return [_button("OK", self.button == "ok", self._click_ok), ("", "      "),
                _button("Cancel", self.button == "cancel", self.cancel)]

    def _kb(self):
        kb = KeyBindings()

        @kb.add("tab")
        @kb.add("s-tab")
        def _(event):
            self._toggle()

        @kb.add("enter")
        def _(event):
            self._accept()

        @kb.add("escape")
        def _(event):
            self.cancel()

        return kb


class ConfirmDialog:
    def __init__(self, on_close):
        self._on_close = on_close
        self.active = False
        self.title = ""
        self.message = ""
        self.ok_label = "OK"        # button labels (overridable per open)
        self.cancel_label = "Cancel"
        self.button = "cancel"      # default to the safe choice for Enter
        self._on_result = None

        self.control = FormattedTextControl(
            self._text, focusable=True, show_cursor=False, key_bindings=self._kb()
        )
        body = HSplit(
            [
                Window(self.control, wrap_lines=True, width=Dimension.exact(WIDTH)),
                Window(height=1),  # spacer
                Window(FormattedTextControl(self._buttons), height=1,
                       align=WindowAlign.CENTER),
            ],
            style="class:dialog",
            padding=0,
        )
        self.container = ConditionalContainer(
            Frame(body, title=lambda: self.title),
            filter=Condition(lambda: self.active),
        )

    def open(self, title, message, on_result, ok_label="OK", cancel_label="Cancel",
             default="cancel"):
        """Show the dialog. ``on_result(True)`` fires for the OK button,
        ``on_result(False)`` for Cancel. ``ok_label``/``cancel_label`` relabel the
        buttons (e.g. two distinct actions) and ``default`` picks which one Enter
        triggers."""
        self.title = title
        self.message = message
        self.ok_label = ok_label
        self.cancel_label = cancel_label
        self.button = default
        self.active = True
        self._on_result = on_result

    def _resolve(self, ok):
        callback = self._on_result
        self.active = False
        self._on_result = None
        self._on_close()
        if callback:
            callback(ok)

    def _toggle(self):
        self.button = "ok" if self.button == "cancel" else "cancel"

    def _text(self):
        return [("class:dialog", " " + self.message)]

    def _buttons(self):
        return [_button(self.ok_label, self.button == "ok", lambda: self._resolve(True)),
                ("", "      "),
                _button(self.cancel_label, self.button == "cancel",
                        lambda: self._resolve(False))]

    def _kb(self):
        kb = KeyBindings()

        # tab / arrows plus vim-style h/l all move between the two buttons; with
        # only two, any direction is just a toggle.
        @kb.add("tab")
        @kb.add("s-tab")
        @kb.add("left")
        @kb.add("right")
        @kb.add("h")
        @kb.add("l")
        def _(event):
            self._toggle()

        @kb.add("enter")
        def _(event):
            self._resolve(self.button == "ok")

        @kb.add("escape")
        @kb.add("c-c")
        def _(event):
            self._resolve(False)

        return kb


class InfoDialog:
    """A centered modal that shows a few lines of read-only text and a single
    OK button. Enter / Esc / Space dismiss it. Used for the About box."""

    def __init__(self, on_close):
        self._on_close = on_close
        self.active = False
        self.title = ""
        self.lines = []  # list[str]

        self.control = FormattedTextControl(
            self._text, focusable=True, show_cursor=False, key_bindings=self._kb()
        )
        body = HSplit(
            [
                Window(self.control, wrap_lines=True, width=Dimension.exact(WIDTH),
                       align=WindowAlign.CENTER),
                Window(height=1),  # spacer
                Window(FormattedTextControl(self._buttons), height=1,
                       align=WindowAlign.CENTER),
            ],
            style="class:dialog",
            padding=0,
        )
        self.container = ConditionalContainer(
            Frame(body, title=lambda: self.title),
            filter=Condition(lambda: self.active),
        )

    def open(self, title, lines):
        self.title = title
        self.lines = list(lines)
        self.active = True

    def _close(self):
        self.active = False
        self._on_close()

    def _text(self):
        out = []
        for line in self.lines:
            out.append(("class:dialog", line + "\n"))
        return out

    def _buttons(self):
        return [_button("OK", True, self._close)]

    def _kb(self):
        kb = KeyBindings()

        @kb.add("enter")
        @kb.add("escape")
        @kb.add("c-c")
        @kb.add(" ")
        def _(event):
            self._close()

        return kb


class FindTextDialog:
    """A find-text form: a phrase to grep for, plus *case sensitive* and *whole
    word* toggles, with OK / Cancel.

    Tab / ↑↓ move between the phrase field, the two checkboxes and the buttons;
    Space toggles a checkbox or activates a button; Enter runs the search; Esc
    cancels. The phrase field edits text in place (cell-width aware)."""

    # focusable rows: 0 phrase, 1 case toggle, 2 whole-word toggle, 3 OK, 4 Cancel
    _PHRASE, _CASE, _WHOLE, _OK, _CANCEL = range(5)
    _LABEL = "Find: "

    def __init__(self, on_close):
        self._on_close = on_close
        self.active = False
        self._on_accept = None
        self.text = ""
        self.pos = 0
        self.case_sensitive = False
        self.whole_word = False
        self.focus = self._PHRASE

        self.control = FormattedTextControl(
            self._render, focusable=True, show_cursor=False, key_bindings=self._kb()
        )
        body = HSplit(
            [Window(self.control, height=6, width=Dimension.exact(WIDTH))],
            style="class:dialog",
            padding=0,
        )
        self.container = ConditionalContainer(
            Frame(body, title=lambda: "Find text"),
            filter=Condition(lambda: self.active),
        )

    def open(self, on_accept, case_sensitive=False, whole_word=False):
        self._on_accept = on_accept
        self.text = ""
        self.pos = 0
        self.case_sensitive = case_sensitive
        self.whole_word = whole_word
        self.focus = self._PHRASE
        self.active = True

    def _close(self):
        self.active = False
        self._on_accept = None
        self._on_close()

    def _accept(self):
        phrase = self.text
        cb = self._on_accept
        cs, ww = self.case_sensitive, self.whole_word
        self._close()
        if cb and phrase.strip():
            cb(phrase, cs, ww)

    # -- rendering ------------------------------------------------------------
    def _phrase_fragments(self, width):
        text, pos = self.text, self.pos
        if self.focus != self._PHRASE:
            shown = cut_to_width(text, width)
            return [("class:dialog.input",
                     shown + " " * max(0, width - text_width(shown)))]
        # focused: block cursor at pos, scrolled so it stays visible
        at = text[pos] if pos < len(text) else " "
        cw = char_width(at) or 1
        start = 0
        while text_width(text[start:pos]) + cw > width:
            start += 1
        before = text[start:pos]
        before_w = text_width(before)
        after = cut_to_width(text[pos + 1:], max(0, width - before_w - cw))
        pad = " " * max(0, width - before_w - cw - text_width(after))
        return [("class:dialog.input", before),
                ("class:dialog.input reverse", at),
                ("class:dialog.input", after + pad)]

    def _render(self):
        field_w = WIDTH - text_width(self._LABEL) - 1
        focus_phrase = _on_click(lambda: setattr(self, "focus", self._PHRASE))
        out = [("class:dialog.label", " " + self._LABEL, focus_phrase)]
        # clicking the phrase field focuses it too
        out += [(s, t, focus_phrase) for s, t in self._phrase_fragments(field_w)]
        out.append(("class:dialog", "\n\n"))

        def check(idx, label, on, action):
            box = "[x] " if on else "[ ] "
            style = "class:dialog.button.focus" if self.focus == idx else "class:dialog"
            return [(style, " " + box + label + " ", _on_click(action)),
                    ("class:dialog", "\n")]

        out += check(self._CASE, "Case sensitive", self.case_sensitive, self._toggle_case)
        out += check(self._WHOLE, "Whole word", self.whole_word, self._toggle_whole)
        out.append(("class:dialog", "\n"))
        # buttons row
        ok = "class:dialog.button.focus" if self.focus == self._OK else "class:dialog.button"
        cancel = ("class:dialog.button.focus" if self.focus == self._CANCEL
                  else "class:dialog.button")
        out += [("class:dialog", "   "), (ok, "  OK  ", _on_click(self._accept)),
                ("class:dialog", "    "),
                (cancel, "  Cancel  ", _on_click(self._close))]
        return out

    def _toggle_case(self):
        self.focus = self._CASE
        self.case_sensitive = not self.case_sensitive

    def _toggle_whole(self):
        self.focus = self._WHOLE
        self.whole_word = not self.whole_word

    # -- editing / keys -------------------------------------------------------
    def _insert(self, s):
        self.text = self.text[:self.pos] + s + self.text[self.pos:]
        self.pos += len(s)

    def _kb(self):
        kb = KeyBindings()
        on_phrase = Condition(lambda: self.focus == self._PHRASE)

        @kb.add(Keys.Any, filter=on_phrase)
        def _(event):
            if event.data and event.data.isprintable():
                self._insert(event.data)

        @kb.add("backspace", filter=on_phrase)
        def _(event):
            if self.pos > 0:
                self.text = self.text[:self.pos - 1] + self.text[self.pos:]
                self.pos -= 1

        @kb.add("delete", filter=on_phrase)
        def _(event):
            if self.pos < len(self.text):
                self.text = self.text[:self.pos] + self.text[self.pos + 1:]

        @kb.add("left", filter=on_phrase)
        def _(event):
            self.pos = max(0, self.pos - 1)

        @kb.add("right", filter=on_phrase)
        def _(event):
            self.pos = min(len(self.text), self.pos + 1)

        @kb.add("home", filter=on_phrase)
        def _(event):
            self.pos = 0

        @kb.add("end", filter=on_phrase)
        def _(event):
            self.pos = len(self.text)

        # field navigation (works from any field)
        @kb.add("tab")
        @kb.add("down")
        def _(event):
            self.focus = (self.focus + 1) % 5

        @kb.add("s-tab")
        @kb.add("up")
        def _(event):
            self.focus = (self.focus - 1) % 5

        @kb.add(" ", filter=~on_phrase)
        def _(event):
            if self.focus == self._CASE:
                self.case_sensitive = not self.case_sensitive
            elif self.focus == self._WHOLE:
                self.whole_word = not self.whole_word
            elif self.focus == self._OK:
                self._accept()
            elif self.focus == self._CANCEL:
                self._close()

        @kb.add("enter")
        def _(event):
            if self.focus == self._CANCEL:
                self._close()
            else:
                self._accept()

        @kb.add("escape")
        @kb.add("c-c")
        def _(event):
            self._close()

        return kb


class ChmodDialog:
    """Edit Unix permissions on a 3×3 grid: rows are owner / group / other, the
    columns are read / write / execute. A live readout shows the symbolic mode
    (``rwxr-xr-x``) and its octal value (``755``).

    Arrow keys (or h/j/k/l) move around the grid and down to the OK / Cancel
    buttons; Tab cycles every cell and button; Space toggles the focused cell or
    presses the focused button; a digit 0-7 typed over a row sets that row's
    three bits at once; Enter applies; Esc cancels. Every cell and button is also
    clickable."""

    # focus indices: 0-8 the grid cells (row-major), 9 OK, 10 Cancel
    _OK, _CANCEL = 9, 10
    _ROWS = ("Owner", "Group", "Other")

    def __init__(self, on_close):
        self._on_close = on_close
        self.active = False
        self.title = ""
        self._on_accept = None
        self.bits = [False] * 9
        self.focus = 0

        self.control = FormattedTextControl(
            self._render, focusable=True, show_cursor=False, key_bindings=self._kb()
        )
        body = HSplit(
            [Window(self.control, height=9, width=Dimension.exact(WIDTH),
                    align=WindowAlign.CENTER)],
            style="class:dialog",
            padding=0,
        )
        self.container = ConditionalContainer(
            Frame(body, title=lambda: self.title),
            filter=Condition(lambda: self.active),
        )

    def open(self, title, mode, on_accept):
        """``mode`` is the initial permission bits (low 9 bits of st_mode);
        ``on_accept(mode_int)`` receives the chosen 0-0o777 value."""
        self.title = title
        self._on_accept = on_accept
        self.bits = [bool(mode & (1 << b)) for b in range(8, -1, -1)]
        self.focus = 0
        self.active = True

    def _close(self):
        self.active = False
        self._on_accept = None
        self._on_close()

    def _mode(self):
        m = 0
        for i, on in enumerate(self.bits):
            if on:
                m |= 1 << (8 - i)
        return m

    def _accept(self):
        cb = self._on_accept
        mode = self._mode()
        self._close()
        if cb:
            cb(mode)

    # -- rendering ------------------------------------------------------------
    def _symbolic(self):
        return "".join(c if on else "-"
                       for c, on in zip("rwxrwxrwx", self.bits))

    def _cell(self, idx, letter):
        on = self.bits[idx]
        if self.focus == idx:
            style = "class:dialog.button.focus"
        elif on:
            style = "class:dialog.button"
        else:
            style = "class:dialog"
        return (style, f" [{letter if on else '-'}] ",
                _on_click(lambda: self._toggle(idx)))

    def _render(self):
        # The window centres each line. The header and the three rows are built
        # to the same width (a 7-col label + three 5-col cells) so they centre to
        # the same offset and the r/w/x columns stay aligned; the readout and
        # buttons centre on their own.
        out = [("class:dialog", "\n")]
        header = " " * 7 + "  r  " + "  w  " + "  x  "  # letters over the cells
        out.append(("class:dialog", header + "\n"))
        for r, name in enumerate(self._ROWS):
            out.append(("class:dialog", f"{name}  "))   # 5-char name + 2 spaces
            for c in range(3):
                out.append(self._cell(r * 3 + c, "rwx"[c]))
            out.append(("class:dialog", "\n"))
        out.append(("class:dialog", "\n"))
        out.append(("class:dialog.label",
                    f"{self._symbolic()}   ({self._mode():03o})\n"))
        out.append(("class:dialog", "\n"))
        ok = ("class:dialog.button.focus" if self.focus == self._OK
              else "class:dialog.button")
        cancel = ("class:dialog.button.focus" if self.focus == self._CANCEL
                  else "class:dialog.button")
        out += [(ok, "  OK  ", _on_click(self._accept)),
                ("class:dialog", "    "),
                (cancel, "  Cancel  ", _on_click(self._close))]
        return out

    # -- editing / navigation -------------------------------------------------
    def _toggle(self, idx):
        self.focus = idx
        self.bits[idx] = not self.bits[idx]

    def _set_row(self, row, value):
        """Set a row's three bits from an octal digit (0-7)."""
        for c in range(3):
            self.bits[row * 3 + c] = bool(value & (1 << (2 - c)))

    def _pos(self):
        if self.focus <= 8:
            return self.focus // 3, self.focus % 3
        return 3, 0 if self.focus == self._OK else 1

    def _move(self, d_row, d_col):
        row, col = self._pos()
        new_row = max(0, min(3, row + d_row))
        new_col = col + d_col
        if new_row <= 2:
            self.focus = new_row * 3 + max(0, min(2, new_col))
        else:
            self.focus = self._OK + max(0, min(1, new_col))

    def _kb(self):
        kb = KeyBindings()

        @kb.add("left")
        @kb.add("h")
        def _(event):
            self._move(0, -1)

        @kb.add("right")
        @kb.add("l")
        def _(event):
            self._move(0, 1)

        @kb.add("up")
        @kb.add("k")
        def _(event):
            self._move(-1, 0)

        @kb.add("down")
        @kb.add("j")
        def _(event):
            self._move(1, 0)

        @kb.add("tab")
        def _(event):
            self.focus = (self.focus + 1) % 11

        @kb.add("s-tab")
        def _(event):
            self.focus = (self.focus - 1) % 11

        @kb.add(" ")
        def _(event):
            if self.focus <= 8:
                self._toggle(self.focus)
            elif self.focus == self._OK:
                self._accept()
            else:
                self._close()

        # type an octal digit to set the focused row at once (e.g. 7 = rwx)
        for d in "01234567":
            @kb.add(d)
            def _(event, d=d):
                if self.focus <= 8:
                    self._set_row(self.focus // 3, int(d))

        @kb.add("enter")
        def _(event):
            if self.focus == self._CANCEL:
                self._close()
            else:
                self._accept()

        @kb.add("escape")
        @kb.add("c-c")
        def _(event):
            self._close()

        return kb
