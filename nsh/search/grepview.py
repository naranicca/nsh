"""Two-pane text search results and match preview."""
import os
import re
import asyncio
from pathlib import Path

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.application.current import get_app
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.layout.margins import Margin
from ..util.aio import run_in_thread
from .. import config
from ..explorer import model
from ..util.widgets import WheelScrollControl
from ..util.width import text_width, cut_to_width


class _GrepScrollbar(Margin):
    def __init__(self, view):
        self.view = view

    def get_width(self, get_ui_content):
        return 1 if len(self.view.lines) > self.view.preview_start + 1 else 0

    def create_margin(self, window_render_info, width, height):
        total = len(self.view.lines)
        visible = max(1, window_render_info.window_height)
        if total <= visible:
            return []
        maximum = max(1, total - visible)
        scroll = min(self.view.preview_start, maximum)
        thumb = max(1, min(height, height * visible // total))
        top = (height - thumb) * scroll // maximum
        thumb_style = ("class:scrollbar.button" if self.view._preview_focused
                       else "class:scrollbar.button.inactive")
        fragments = []
        for row in range(height):
            fragments.append((thumb_style if top <= row < top + thumb
                              else "class:scrollbar.background", " "))
            if row < height - 1:
                fragments.append(("", "\n"))
        return fragments


class GrepView:
    def __init__(self, app):
        self.app = app
        self.items = []
        self.loading = True
        self.cursor = 0
        self.list_start = 0
        self.lines = []
        self.preview_start = 0
        self.match_index = 0
        self.query = ""
        self._preview_focused = False
        self.list_control = WheelScrollControl(
            lambda direction: self.move(direction),
            on_click=self._on_list_click,
            text=self._list, focusable=True, show_cursor=False,
            key_bindings=self._kb())
        self.preview_control = WheelScrollControl(
            lambda direction: self._scroll_preview(direction),
            on_click=lambda event: self._focus_preview(),
            text=self._preview, focusable=True, show_cursor=False,
            key_bindings=self._preview_kb())
        self.query_header = VSplit([
            Window(FormattedTextControl(
                lambda: [("class:search.prompt", " grep ▸ ")]),
                width=8, height=1, style="class:search.prompt"),
            Window(FormattedTextControl(lambda: [("class:search.input", self.query)]),
                   height=1, style="class:search.input"),
        ])
        self.file_header = Window(
            FormattedTextControl(self._file_header), height=1,
            # Window-level style fills the complete pane width, matching the
            # Explorer preview header instead of highlighting only the path.
            style=lambda: ("class:preview.header.focus" if self._preview_focused
                            else "class:preview.header"))
        self._left = HSplit([self.query_header,
                             Window(self.list_control, wrap_lines=False)])
        self.preview_window = Window(
            self.preview_control, wrap_lines=False,
            right_margins=[_GrepScrollbar(self)])
        self._right = HSplit([self.file_header, self.preview_window])
        self.container = VSplit([
            self._left,
            Window(width=1, char="│", style="class:preview.border"),
            self._right,
        ])
        self._update_widths()

    def start(self, query, case_sensitive=False, whole_word=False):
        self.query = query
        flags = 0 if case_sensitive else re.I
        pattern = (r"\b(?:" + re.escape(query) + r")\b"
                   if whole_word else re.escape(query))
        rx = re.compile(pattern, flags)
        self.items = []
        self.cursor = 0
        self.list_start = 0
        self.app.switch_mode("grep")
        asyncio.ensure_future(self._collect(Path(self.app.cwd), rx))

    async def _collect(self, root, rx):
        """Scan incrementally so results redraw while files arrive."""
        try:
            paths = await run_in_thread(lambda: [p for p in root.rglob("*") if p.is_file()])
            for path in paths:
                result = await run_in_thread(self._match_file, path, rx)
                if result:
                    self.items.append(result)
                    self._load_preview()
                    self.app.invalidate()
        except (OSError, ValueError):
            pass
        self.loading = False
        self.app.invalidate()

    @staticmethod
    def _match_file(path, rx):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        lines = text.splitlines()
        hits = [i for i, line in enumerate(lines, 1) if rx.search(line)]
        return (path, hits, lines) if hits else None

    def _load_preview(self):
        if self.items:
            _, hits, lines = self.items[self.cursor]
            self.lines = lines
            # Keep the first match near the top with a little context above it,
            # matching the explorer preview's useful initial scroll position.
            self.preview_start = max(0, (hits[0] if hits else 1) - 4)
            self.match_index = 0
        else:
            self.lines = []
            self.preview_start = 0
        self.app.invalidate()

    def move(self, delta):
        if self.items:
            self.cursor = max(0, min(len(self.items) - 1, self.cursor + delta))
            try:
                visible = max(1, get_app().output.get_size().rows - 4)
            except Exception:
                visible = 20
            if self.cursor < self.list_start:
                self.list_start = self.cursor
            elif self.cursor >= self.list_start + visible:
                self.list_start = self.cursor - visible + 1
            self._load_preview()

    def _list(self):
        self._update_widths()
        out = []
        try:
            visible = max(1, get_app().output.get_size().rows - 4)
        except Exception:
            visible = 20
        if self.items:
            self.list_start = max(0, min(self.list_start,
                                         max(0, len(self.items) - visible)))
        for i, (path, hits, _) in enumerate(
                self.items[self.list_start:self.list_start + visible], self.list_start):
            # Explorer applies the terminal ``reverse`` attribute to the whole
            # cursor row; use the same treatment here instead of only changing
            # the foreground/background theme color.
            style = ("class:search.results reverse" if i == self.cursor
                     else "class:search.results")
            rel = Path(os.path.relpath(path, self.app.cwd))
            prefix = "› " if i == self.cursor else "  "
            out.append((style, prefix))
            parts = list(rel.parts)
            for part in parts[:-1]:
                out.append(("class:explorer.dir" + (" reverse" if i == self.cursor else ""), part + os.sep))
            name = parts[-1] if parts else str(path.name)
            suffix = path.suffix.lower()
            name_style = ("class:explorer.image" if suffix in model.IMAGE_EXTS
                          else "class:explorer.exec" if suffix in model.WIN_EXEC_EXTS
                          else "class:explorer.file")
            row_suffix = " reverse" if i == self.cursor else ""
            # Keep the single separator cell part of the filename fragment so
            # it retains the filename colour instead of the line-number colour.
            out.append((name_style + row_suffix, name + " "))
            out.append(("#ffff00" + (" reverse" if i == self.cursor else ""),
                        ",".join(map(str, hits)) + "\n"))
        if out:
            return out
        return [("class:search.results",
                 "(searching...)" if self.loading else "(no matches)")]

    def _preview(self):
        self._update_widths()
        if not self.items:
            return [("class:preview.text", "")]
        _, hits, _ = self.items[self.cursor]
        hitset = set(hits)
        current_hit = hits[self.match_index] if hits else None
        height = (self.preview_window.render_info.window_height
                  if self.preview_window.render_info else len(self.lines))
        height = max(1, height)
        visible = enumerate(self.lines[self.preview_start:self.preview_start + height],
                            self.preview_start + 1)
        width = self.preview_control.last_width or 80
        result = []
        for i, line in visible:
            style = ("class:search-match-current" if i == current_hit
                     else "class:search.match" if i in hitset
                     else "class:preview.text")
            text = cut_to_width(f"{i:5} {line}", width)
            text += " " * max(0, width - text_width(text))
            result.append((style, text + "\n"))
        return result

    def _file_header(self):
        header_style = ("class:preview.header.focus" if self._preview_focused
                        else "class:preview.header")
        if not self.items:
            return [(header_style, " ")]
        path, hits, _ = self.items[self.cursor]
        rel = os.path.relpath(path, self.app.cwd)
        count = f"  [{self.match_index + 1}/{len(hits)}]" if hits else ""
        return [(header_style, " " + rel + count)]

    def _kb(self):
        kb = KeyBindings()
        # Reuse the Explorer action menu binding (normally Tab) while the
        # results list has focus.
        menu_key = self.app.keys.get("menu")
        if menu_key:
            try:
                kb.add(menu_key)(lambda e: self._open_action_menu())
            except Exception:
                pass
        kb.add("up")(lambda e: self.move(-1))
        kb.add("down")(lambda e: self.move(1))
        kb.add("k")(lambda e: self.move(-1))
        kb.add("j")(lambda e: self.move(1))
        kb.add("g")(lambda e: self._jump_result(0))
        kb.add("G")(lambda e: self._jump_result(len(self.items) - 1))
        kb.add("l")(lambda e: self._focus_preview())
        kb.add(":")(lambda e: self.app.switch_mode("shell"))
        kb.add("c-j")(lambda e: self._send_to_shell())
        kb.add("escape")(lambda e: self.app.switch_mode("explorer"))
        return kb

    def _jump_result(self, index):
        if not self.items:
            return
        self.cursor = max(0, min(len(self.items) - 1, index))
        self.list_start = self.cursor
        self._load_preview()

    def _preview_kb(self):
        kb = KeyBindings()
        kb.add("j")(lambda e: self._scroll_preview(1))
        kb.add("down")(lambda e: self._scroll_preview(1))
        kb.add("k")(lambda e: self._scroll_preview(-1))
        kb.add("up")(lambda e: self._scroll_preview(-1))
        kb.add("h")(lambda e: self._focus_list())
        kb.add("left")(lambda e: self._focus_list())
        kb.add("escape")(lambda e: self._focus_list())
        kb.add("n")(lambda e: self._move_match(1))
        kb.add("f3")(lambda e: self._move_match(1))
        kb.add("N")(lambda e: self._move_match(-1))
        # prompt_toolkit has no portable ``s-f3`` name on Win32; terminals
        # commonly encode Shift+F3 as an escape-prefixed F3 sequence.
        kb.add("escape", "f3")(lambda e: self._move_match(-1))
        return kb

    def _focus_preview(self):
        self._preview_focused = True
        self._update_widths()
        self.app.application.layout.focus(self.preview_control)

    def _on_list_click(self, event):
        self._focus_list()
        if event.event_type != MouseEventType.MOUSE_DOWN:
            return
        # The control is below the query header, so its y coordinate already
        # starts at the first result row.
        index = self.list_start + max(0, event.position.y)
        if 0 <= index < len(self.items):
            self.cursor = index
            self._load_preview()

    def _focus_list(self):
        self._preview_focused = False
        self._update_widths()
        self.app.application.layout.focus(self.list_control)

    def _update_widths(self):
        zoomed = bool(getattr(self.app, "zoom", False))
        left_weight = 1 if zoomed and self._preview_focused else (9 if zoomed else 1)
        right_weight = 9 if zoomed and self._preview_focused else 1
        self._left.width = Dimension(min=0, preferred=0, weight=left_weight)
        self._right.width = Dimension(min=0, preferred=0, weight=right_weight)

    def _scroll_preview(self, delta):
        if self.lines:
            self.preview_start = max(0, min(self._max_preview_start(),
                                            self.preview_start + delta))
            self.app.invalidate()

    def _max_preview_start(self):
        height = (self.preview_window.render_info.window_height
                  if self.preview_window.render_info else 1)
        return max(0, len(self.lines) - max(1, height))

    def _move_match(self, direction):
        if not self.items:
            return
        hits = self.items[self.cursor][1]
        if not hits:
            return
        self.match_index = (self.match_index + (1 if direction > 0 else -1)) % len(hits)
        target = hits[self.match_index]
        self.preview_start = min(self._max_preview_start(), max(0, target - 4))
        self.app.invalidate()

    def _send_to_shell(self):
        if not self.items:
            return
        self.app.shell_insert_paths([self.items[self.cursor][0]])

    def _open_action_menu(self):
        """Open Explorer's menu for the file represented by the result row."""
        explorer = self.app.explorer
        if self.items:
            path = self.items[self.cursor][0]
            # The action menu force-selects the Explorer cursor when there is
            # no selection. Clear any stale selection from the previous view,
            # otherwise it can keep the menu anchored to the first row.
            explorer.selected.clear()
            # A recursive result may not be present in the current Explorer
            # listing.  Refresh the listing first so the selected result can be
            # resolved instead of falling back to row zero.
            try:
                explorer.refresh()
            except Exception:
                pass
            for index, entry in enumerate(explorer.entries):
                if entry.path == path:
                    explorer.cursor = index
                    break
        target = self.items[self.cursor][0] if self.items else None
        explorer.open_command_menu(target_path=target)
