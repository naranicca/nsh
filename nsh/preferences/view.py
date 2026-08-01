"""Searchable, full-screen Preferences view."""
from dataclasses import dataclass

from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.mouse_events import MouseEventType

from .. import config
from ..util.widgets import WheelScrollControl
from ..util.width import cut_to_width, pad_to_width


@dataclass(frozen=True)
class PreferenceEntry:
    section: str
    category: str
    name: str
    value: str
    blank_resets: bool = False
    modified: bool = False
    blank_unbinds: bool = False


class PreferencesView:
    """A VS Code-style searchable list of every supported setting."""

    def __init__(self, app):
        self.app = app
        self.entries = []
        self.filtered = []
        self.cursor = 0
        self._top = 0
        self.query_buffer = Buffer(multiline=False, on_text_changed=self._on_query)
        self.query_control = BufferControl(self.query_buffer, key_bindings=self._keys())
        self.list_control = WheelScrollControl(
            lambda delta: self.move(delta * 3), on_click=self._on_mouse,
            text=self._list_text, focusable=False, show_cursor=False)
        self.list_window = Window(
            self.list_control, wrap_lines=False, style="class:preferences.list")
        self.container = HSplit([
            Window(FormattedTextControl(self._heading), height=2,
                   style="class:preferences.header"),
            VSplit([
                Window(FormattedTextControl(
                    lambda: [("class:preferences.search.label", " Search ")]),
                    width=8, height=1),
                Window(self.query_control, height=1,
                       style="class:preferences.search.input"),
                Window(FormattedTextControl(self._counter), width=12, height=1,
                       align="right", style="class:preferences.search.count"),
            ]),
            Window(height=1, char="─", style="class:preview.border"),
            self.list_window,
        ])

    def start(self):
        self.query_buffer.reset()
        self.refresh()

    def resume(self):
        self.refresh(keep_selection=True)
        self.app.application.layout.focus(self.query_control)
        self.app.invalidate()

    def refresh(self, keep_selection=False):
        selected = self.current() if keep_selection else None
        colors, keys, settings, _warning = config.load_user_config()
        entries = []
        for name, default in config.DEFAULT_SETTINGS.items():
            value = settings.get(name, default)
            entries.append(PreferenceEntry(
                "general", "Variables", name, value,
                modified=value != default))
        for name, default in config.STYLE_DEFAULTS.items():
            value = colors.get(name, default)
            entries.append(PreferenceEntry(
                "colors", "Colors", name, value, True, value != default))
        for name, default in config.DEFAULT_KEYS.items():
            value = keys.get(name, default)
            modified = value != default
            entries.append(PreferenceEntry(
                "keys", "Shortcuts", name,
                "space" if value == " " else (value or "(unbound)"),
                modified=modified, blank_unbinds=True))
        self.entries = entries
        self._filter()
        if selected is not None:
            for i, entry in enumerate(self.filtered):
                if (entry.section, entry.name) == (selected.section, selected.name):
                    self.cursor = i
                    break
        self._clamp_cursor()

    def _on_query(self, _buffer):
        self.cursor = 0
        self._top = 0
        self._filter()
        self.app.invalidate()

    def clear_search(self):
        """Clear the query and restore the full list immediately.

        ``Buffer.reset()`` does not consistently fire ``on_text_changed`` when
        invoked from a key handler, so update the filtered model explicitly.
        """
        self.query_buffer.reset()
        self.cursor = 0
        self._top = 0
        self._filter()
        self.app.invalidate()

    def _filter(self):
        words = self.query_buffer.text.lower().split()
        self.filtered = [entry for entry in self.entries
                         if all(word in (entry.category + " " + entry.name + " "
                                        + entry.value).lower() for word in words)]
        self._clamp_cursor()

    def _clamp_cursor(self):
        self.cursor = max(0, min(self.cursor, len(self.filtered) - 1))

    def current(self):
        return self.filtered[self.cursor] if self.filtered else None

    def move(self, delta):
        if self.filtered:
            self.cursor = max(0, min(len(self.filtered) - 1, self.cursor + delta))
            self.app.invalidate()

    def edit(self):
        entry = self.current()
        if entry is not None:
            self.app._edit_preference(
                entry.section, entry.name, entry.value, self.resume,
                blank_resets=entry.blank_resets, modified=entry.modified,
                blank_unbinds=entry.blank_unbinds)

    def _visible_height(self):
        info = getattr(self.list_window, "render_info", None)
        return info.window_height if info is not None and info.window_height else 20

    @staticmethod
    def _term_cols():
        try:
            return get_app().output.get_size().columns
        except Exception:
            return 80

    @staticmethod
    def _heading():
        return [("class:preferences.title", " Preferences\n"),
                ("class:preferences.subtitle",
                 " Search settings by category, name, or current value")]

    def _counter(self):
        return [("class:preferences.search.count",
                 f" {len(self.filtered)}/{len(self.entries)} ")]

    def _list_text(self):
        if not self.filtered:
            return [("class:preferences.empty", "  No settings found")]
        height = self._visible_height()
        if self.cursor < self._top:
            self._top = self.cursor
        elif self.cursor >= self._top + height:
            self._top = self.cursor - height + 1
        self._top = max(0, min(self._top, max(0, len(self.filtered) - height)))
        cols = self._term_cols()
        category_w = 12
        name_w = max(18, min(36, cols // 3))
        value_w = max(4, cols - category_w - name_w - 9)
        out = []
        shown = self.filtered[self._top:self._top + height]
        for offset, entry in enumerate(shown):
            index = self._top + offset
            selected = index == self.cursor
            style = ("class:preferences.row.selected" if selected
                     else "class:preferences.row")
            changed = "* " if entry.modified else "  "
            mark = "› " if selected else "  "
            category = pad_to_width(entry.category, category_w)
            name = pad_to_width(cut_to_width(entry.name, name_w), name_w)
            value = pad_to_width(cut_to_width(entry.value or "(empty)", value_w),
                                 value_w)
            out.append((style, f"{mark}{changed}{category}  {name}  {value}"))
            if offset != len(shown) - 1:
                out.append(("", "\n"))
        return out

    def _on_mouse(self, mouse_event):
        if mouse_event.event_type != MouseEventType.MOUSE_DOWN:
            return
        index = self._top + mouse_event.position.y
        if not 0 <= index < len(self.filtered):
            return
        self.cursor = index
        if self.app.double_click("preferences", index):
            self.edit()
        else:
            self.app.invalidate()

    def _keys(self):
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("c-p")
        def _(event):
            self.move(-1)

        @kb.add("down")
        @kb.add("c-n")
        def _(event):
            self.move(1)

        @kb.add("pageup")
        def _(event):
            self.move(-self._visible_height())

        @kb.add("pagedown")
        def _(event):
            self.move(self._visible_height())

        @kb.add("enter")
        def _(event):
            self.edit()

        @kb.add("c-o")
        def _(event):
            self.app.edit_preferences_file()

        @kb.add("escape")
        @kb.add("c-c")
        def _(event):
            if self.query_buffer.text:
                self.clear_search()
            else:
                self.app.close_preferences()

        return kb
