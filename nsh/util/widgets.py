"""Small reusable UI widgets."""
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEventType


def visible_slice(window, total, cursor, top, margin=1, fallback=40):
    """Half-open row range ``(top, end)`` to render for a long cursor list, so
    only the on-screen rows are materialised instead of all ``total`` of them.

    ``cursor`` is kept within ``margin`` rows of the window edges, scrolling
    ``top`` (the previous origin) as little as possible. The height comes from
    ``window.render_info`` — it lags one frame, so ``fallback`` covers the very
    first render. The view also reports the cursor as ``cursor - top`` so the
    Window doesn't try to scroll the (already windowed) content itself.
    """
    ri = window.render_info
    height = ri.window_height if (ri is not None and ri.window_height) else fallback
    height = max(1, height)
    if total <= height:
        return 0, total
    if cursor < top + margin:
        top = cursor - margin
    elif cursor >= top + height - margin:
        top = cursor - height + margin + 1
    top = max(0, min(top, total - height))
    return top, min(total, top + height)


class WheelScrollControl(FormattedTextControl):
    """A :class:`FormattedTextControl` that reports mouse-wheel scrolls and,
    optionally, clicks.

    ``on_scroll(direction)`` is called with ``-1`` (up) or ``+1`` (down). When
    ``on_click`` is given it receives the raw :class:`MouseEvent` on press (the
    caller maps ``mouse_event.position`` to a row); every plain mouse event is
    then consumed here so the default cursor-positioning doesn't fight that
    handling. Without ``on_click`` all non-wheel events fall through to the
    default.
    """

    def __init__(self, on_scroll, on_click=None, **kwargs):
        self._on_scroll = on_scroll
        self._on_click = on_click
        super().__init__(**kwargs)

    def mouse_handler(self, mouse_event):
        et = mouse_event.event_type
        if et == MouseEventType.SCROLL_UP:
            self._on_scroll(-1)
            return None
        if et == MouseEventType.SCROLL_DOWN:
            self._on_scroll(1)
            return None
        if self._on_click is not None:
            if et == MouseEventType.MOUSE_DOWN:
                self._on_click(mouse_event)
            return None  # swallow down/up/move so default handling stays out
        return super().mouse_handler(mouse_event)
