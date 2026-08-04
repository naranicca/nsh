"""Centered modal dialogs.

``InputDialog`` — a text field with OK / Cancel buttons.
``ConfirmDialog`` — a message with Yes / No buttons.
``InfoDialog`` — read-only text with an OK button.
``FindTextDialog`` — a phrase plus case-sensitive / whole-word toggles.
``ChmodDialog`` — a 3×3 rwx grid (owner/group/other) with a live octal readout.

All: arrow keys / Tab move between fields/buttons, Enter runs the primary action,
Esc dismisses.
"""
import re

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
from prompt_toolkit.layout.processors import ConditionalProcessor, PasswordProcessor
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
        self._on_extra = None       # optional third action (e.g. Reset Default)
        self.extra_label = None
        self.password = False
        self.buffer = Buffer(multiline=False, on_text_changed=self._text_changed)

        self.control = BufferControl(
            self.buffer, key_bindings=self._kb(),
            input_processors=[ConditionalProcessor(
                PasswordProcessor(), Condition(lambda: self.password))])
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

    def open(self, title, text, cursor, on_accept, on_change=None, on_cancel=None,
             password=False, extra_label=None, on_extra=None):
        self.title = title
        self._on_accept = on_accept
        # set the live-edit hooks before seeding the text, so a non-empty initial
        # value (rare) reaches on_change too
        self._on_change = on_change
        self._on_cancel = on_cancel
        self._on_extra = on_extra
        self.extra_label = extra_label
        self.password = password
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
        self._on_extra = None
        self.extra_label = None
        self.password = False
        self.buffer.text = ""  # do not retain passwords after the dialog closes
        self._on_close()

    def _toggle(self):
        order = ["ok"] + (["extra"] if self._on_extra else []) + ["cancel"]
        self.button = order[(order.index(self.button) + 1) % len(order)]

    def _click_extra(self):
        callback = self._on_extra
        self._close()
        if callback:
            callback()

    def _click_ok(self):
        self.button = "ok"
        self._accept()

    def _buttons(self):
        buttons = [_button("OK", self.button == "ok", self._click_ok)]
        if self._on_extra:
            buttons += [("", "   "),
                        _button(self.extra_label or "Action",
                                self.button == "extra", self._click_extra)]
        buttons += [("", "   "),
                    _button("Cancel", self.button == "cancel", self.cancel)]
        return buttons

    def _kb(self):
        kb = KeyBindings()

        @kb.add("tab")
        @kb.add("s-tab")
        def _(event):
            self._toggle()

        @kb.add("enter")
        def _(event):
            if self.button == "extra":
                self._click_extra()
            else:
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


class ProgressDialog:
    """Blocking modal progress display with one cancellable operation."""

    def __init__(self, on_close):
        self._on_close = on_close
        self.active = False
        self.title = ""
        self.label = ""
        self.done = 0
        self.total = 0
        self._on_cancel = None
        self.control = FormattedTextControl(
            self._text, focusable=True, show_cursor=False, key_bindings=self._kb())
        body = HSplit([
            Window(self.control, height=3, width=Dimension.exact(WIDTH)),
            Window(FormattedTextControl(self._buttons), height=1,
                   align=WindowAlign.CENTER),
        ], style="class:dialog")
        self.container = ConditionalContainer(
            Frame(body, title=lambda: self.title),
            filter=Condition(lambda: self.active))

    def open(self, title, label, on_cancel):
        self.title, self.label = title, label
        self.done = self.total = 0
        self._on_cancel = on_cancel
        self.active = True

    def update(self, done, total):
        self.done = max(0, int(done or 0))
        self.total = max(0, int(total or 0))

    def close(self):
        if not self.active:
            return
        self.active = False
        self._on_cancel = None
        self._on_close()

    def cancel(self):
        callback = self._on_cancel
        if callback:
            callback()

    def _text(self):
        ratio = min(1.0, self.done / self.total) if self.total else 0.0
        width = 34
        filled = int(width * ratio)
        amount = (f"{ratio * 100:5.1f}%  {self.done} / {self.total} bytes"
                  if self.total else f"{self.done} bytes")
        return [
            ("class:dialog", " " + cut_to_width(self.label, WIDTH - 2) + "\n"),
            ("class:shell.elapsed.ok", " [" + "=" * filled),
            ("class:shell.queued", "-" * (width - filled) + "]\n"),
            ("class:dialog", " " + amount),
        ]

    def _buttons(self):
        return [_button("Cancel", True, self.cancel)]

    def _kb(self):
        kb = KeyBindings()
        kb.add("enter")(lambda event: self.cancel())
        kb.add("escape")(lambda event: self.cancel())
        kb.add("c-c")(lambda event: self.cancel())
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

    The Mode field accepts octal (``755``), symbolic bits (``rwxr-xr-x``), or
    chmod expressions (``u+x``, ``go-w``, ``a=rw``). The grid stays synchronized
    with valid text. Arrow keys (or h/j/k/l) move around the grid; Tab cycles
    through the field, cells and buttons; Space toggles a cell; Enter applies;
    Esc cancels. Every cell and button is also clickable."""

    # focus indices: 0 text, 1-9 grid cells, 10 OK, 11 Cancel
    _TEXT, _OK, _CANCEL = 0, 10, 11
    _ROWS = ("Owner", "Group", "Other")

    def __init__(self, on_close):
        self._on_close = on_close
        self.active = False
        self.title = ""
        self._on_accept = None
        self.bits = [False] * 9
        self.focus = self._TEXT
        self.mode_text = ""
        self.text_pos = 0
        self._base_mode = 0
        self._text_pristine = True
        self.error = ""

        self.control = FormattedTextControl(
            self._render, focusable=True, show_cursor=False, key_bindings=self._kb()
        )
        body = HSplit(
            [Window(self.control, height=11, width=Dimension.exact(WIDTH),
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
        self._base_mode = mode & 0o777
        self.bits = [bool(mode & (1 << b)) for b in range(8, -1, -1)]
        self.focus = self._TEXT
        self.mode_text = f"{self._base_mode:03o}"
        self.text_pos = len(self.mode_text)
        self._text_pristine = True
        self.error = ""
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
        mode = self._parse_mode_text(self.mode_text)
        if mode is None:
            self.error = "Use 755, rwxr-xr-x, or u+x"
            return
        self._set_mode(mode)
        cb = self._on_accept
        self._close()
        if cb:
            cb(mode)

    # -- rendering ------------------------------------------------------------
    def _symbolic(self):
        return "".join(c if on else "-"
                       for c, on in zip("rwxrwxrwx", self.bits))

    def _set_mode(self, mode):
        self.bits = [bool(mode & (1 << b)) for b in range(8, -1, -1)]

    def _parse_mode_text(self, text):
        """Parse octal, rwx bits, or a small chmod-compatible expression."""
        value = text.strip()
        if re.fullmatch(r"0?[0-7]{3}", value):
            return int(value, 8) & 0o777
        if re.fullmatch(r"[r-][w-][x-][r-][w-][x-][r-][w-][x-]", value):
            return sum(1 << (8 - i) for i, char in enumerate(value) if char != "-")
        if not value:
            return None
        mode = self._base_mode
        masks = {"r": 4, "w": 2, "x": 1}
        for clause in value.split(","):
            match = re.fullmatch(r"([ugoa]*)([+=-])([rwx]*)", clause)
            if not match:
                return None
            who, operation, permissions = match.groups()
            who = who or "a"
            groups = {0 if char == "u" else 1 if char == "g" else 2
                      for char in who if char in "ugo"}
            if "a" in who:
                groups = {0, 1, 2}
            value_bits = sum(masks[char] for char in permissions)
            for group in groups:
                shift = (2 - group) * 3
                group_mask = 7 << shift
                if operation == "=":
                    mode = (mode & ~group_mask) | (value_bits << shift)
                elif operation == "+":
                    mode |= value_bits << shift
                else:
                    mode &= ~(value_bits << shift)
        return mode

    def _cell(self, idx, letter):
        on = self.bits[idx - 1]
        if self.focus == idx:
            style = "class:dialog.button.focus"
        elif on:
            style = "class:dialog.button"
        else:
            style = "class:dialog"
        return (style, f" [{letter if on else '-'}] ",
                _on_click(lambda: self._toggle(idx)))

    def _mode_field(self):
        width = 18
        text = self.mode_text
        if self.focus != self._TEXT:
            shown = cut_to_width(text, width)
            return [("class:dialog.input",
                     shown + " " * max(0, width - text_width(shown)))]
        pos = min(self.text_pos, len(text))
        start = max(0, pos - width + 1)
        at = text[pos] if pos < len(text) else " "
        before = text[start:pos]
        after = cut_to_width(text[pos + 1:], max(0, width - len(before) - 1))
        pad = " " * max(0, width - len(before) - 1 - len(after))
        return [("class:dialog.input", before),
                ("class:dialog.input reverse", at),
                ("class:dialog.input", after + pad)]

    def _render(self):
        # The window centres each line. The header and the three rows are built
        # to the same width (a 7-col label + three 5-col cells) so they centre to
        # the same offset and the r/w/x columns stay aligned; the readout and
        # buttons centre on their own.
        out = [("class:dialog", "\nMode  ")]
        out += self._mode_field()
        out.append(("class:dialog", "\n"))
        header = " " * 7 + "  r  " + "  w  " + "  x  "  # letters over the cells
        out.append(("class:dialog", header + "\n"))
        for r, name in enumerate(self._ROWS):
            out.append(("class:dialog", f"{name}  "))   # 5-char name + 2 spaces
            for c in range(3):
                out.append(self._cell(1 + r * 3 + c, "rwx"[c]))
            out.append(("class:dialog", "\n"))
        out.append(("class:dialog", "\n"))
        message = self.error or f"{self._symbolic()}   ({self._mode():03o})"
        out.append(("class:dialog.label", message + "\n"))
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
        bit = idx - 1
        self.bits[bit] = not self.bits[bit]
        self._sync_text()

    def _set_row(self, row, value):
        """Set a row's three bits from an octal digit (0-7)."""
        for c in range(3):
            self.bits[row * 3 + c] = bool(value & (1 << (2 - c)))
        self._sync_text()

    def _sync_text(self):
        self.mode_text = f"{self._mode():03o}"
        self.text_pos = len(self.mode_text)
        self._text_pristine = True
        self.error = ""

    def _edit_text(self, char):
        if self._text_pristine:
            self.mode_text = ""
            self.text_pos = 0
            self._text_pristine = False
        self.mode_text = (self.mode_text[:self.text_pos] + char
                          + self.mode_text[self.text_pos:])
        self.text_pos += len(char)
        parsed = self._parse_mode_text(self.mode_text)
        if parsed is not None:
            self._set_mode(parsed)
            self.error = ""

    def _backspace_text(self):
        self._text_pristine = False
        if self.text_pos:
            self.mode_text = (self.mode_text[:self.text_pos - 1]
                              + self.mode_text[self.text_pos:])
            self.text_pos -= 1
        parsed = self._parse_mode_text(self.mode_text)
        if parsed is not None:
            self._set_mode(parsed)

    def _pos(self):
        if 1 <= self.focus <= 9:
            bit = self.focus - 1
            return bit // 3, bit % 3
        if self.focus == self._TEXT:
            return -1, 0
        return 3, 0 if self.focus == self._OK else 1

    def _move(self, d_row, d_col):
        row, col = self._pos()
        new_row = max(0, min(3, row + d_row))
        new_col = col + d_col
        if new_row < 0:
            self.focus = self._TEXT
        elif new_row <= 2:
            self.focus = 1 + new_row * 3 + max(0, min(2, new_col))
        else:
            self.focus = self._OK + max(0, min(1, new_col))

    def _kb(self):
        kb = KeyBindings()
        on_text = Condition(lambda: self.focus == self._TEXT)

        @kb.add(Keys.Any, filter=on_text)
        def _(event):
            if event.data and event.data.isprintable():
                self._edit_text(event.data)

        @kb.add("backspace", filter=on_text)
        def _(event):
            self._backspace_text()

        @kb.add("delete", filter=on_text)
        def _(event):
            self._text_pristine = False
            if self.text_pos < len(self.mode_text):
                self.mode_text = (self.mode_text[:self.text_pos]
                                  + self.mode_text[self.text_pos + 1:])
            parsed = self._parse_mode_text(self.mode_text)
            if parsed is not None:
                self._set_mode(parsed)

        @kb.add("left", filter=on_text)
        def _(event):
            self._text_pristine = False
            self.text_pos = max(0, self.text_pos - 1)

        @kb.add("right", filter=on_text)
        def _(event):
            self._text_pristine = False
            self.text_pos = min(len(self.mode_text), self.text_pos + 1)

        @kb.add("left", filter=~on_text)
        @kb.add("h", filter=~on_text)
        def _(event):
            self._move(0, -1)

        @kb.add("right", filter=~on_text)
        @kb.add("l", filter=~on_text)
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
            self.focus = (self.focus + 1) % 12

        @kb.add("s-tab")
        def _(event):
            self.focus = (self.focus - 1) % 12

        @kb.add(" ", filter=~on_text)
        def _(event):
            if 1 <= self.focus <= 9:
                self._toggle(self.focus)
            elif self.focus == self._OK:
                self._accept()
            else:
                self._close()

        # type an octal digit to set the focused row at once (e.g. 7 = rwx)
        for d in "01234567":
            @kb.add(d, filter=~on_text)
            def _(event, d=d):
                if 1 <= self.focus <= 9:
                    self._set_row((self.focus - 1) // 3, int(d))

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
