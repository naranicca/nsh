"""Small reusable UI widgets."""
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEventType


class WheelScrollControl(FormattedTextControl):
    """A :class:`FormattedTextControl` that reports mouse-wheel scrolls.

    ``on_scroll(direction)`` is called with ``-1`` (up) or ``+1`` (down). All
    other mouse events fall through to the default handling.
    """

    def __init__(self, on_scroll, **kwargs):
        self._on_scroll = on_scroll
        super().__init__(**kwargs)

    def mouse_handler(self, mouse_event):
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._on_scroll(-1)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._on_scroll(1)
            return None
        return super().mouse_handler(mouse_event)
