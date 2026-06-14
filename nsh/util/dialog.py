"""A centered modal input dialog: a text field with OK / Cancel buttons.

Arrow keys edit the field; Tab toggles which button Enter triggers; Enter runs
the active button (OK -> accept, Cancel -> dismiss); Esc dismisses.
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


class InputDialog:
    WIDTH = 50

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
                       width=Dimension.exact(self.WIDTH)),
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

    # -- lifecycle ------------------------------------------------------------
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

    # -- rendering ------------------------------------------------------------
    def _buttons(self):
        def b(name, label):
            style = "class:dialog.button.focus" if self.button == name else "class:dialog.button"
            return (style, f"  {label}  ")
        return [b("ok", "OK"), ("", "      "), b("cancel", "Cancel")]

    # -- keys -----------------------------------------------------------------
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
