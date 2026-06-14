"""Centered modal dialogs.

``InputDialog`` — a text field with OK / Cancel buttons.
``ConfirmDialog`` — a message with Yes / No buttons.

Both: arrow keys / Tab move between/choose buttons, Enter runs the active one,
Esc dismisses (cancel / No).
"""
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    HSplit,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame

WIDTH = 50


def _button(label, active):
    style = "class:dialog.button.focus" if active else "class:dialog.button"
    return (style, f"  {label}  ")


class InputDialog:
    def __init__(self, on_close):
        self._on_close = on_close
        self.active = False
        self.title = ""
        self.button = "ok"          # which button Enter triggers: "ok" | "cancel"
        self._on_accept = None
        self.buffer = Buffer(multiline=False)

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

    def open(self, title, text, cursor, on_accept):
        self.title = title
        self._on_accept = on_accept
        self.button = "ok"
        self.active = True
        self.buffer.text = text
        self.buffer.cursor_position = max(0, min(cursor, len(text)))

    def cancel(self):
        self._on_accept = None
        self._close()

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
        self._on_close()

    def _toggle(self):
        self.button = "cancel" if self.button == "ok" else "ok"

    def _buttons(self):
        return [_button("OK", self.button == "ok"), ("", "      "),
                _button("Cancel", self.button == "cancel")]

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

    def open(self, title, message, on_result):
        self.title = title
        self.message = message
        self.button = "cancel"
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
        return [_button("OK", self.button == "ok"), ("", "      "),
                _button("Cancel", self.button == "cancel")]

    def _kb(self):
        kb = KeyBindings()

        @kb.add("tab")
        @kb.add("s-tab")
        @kb.add("left")
        @kb.add("right")
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
