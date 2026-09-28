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
from ..util.aio import run_in_thread
from .. import config
from ..explorer import model


class GrepView:
    def __init__(self, app):
        self.app = app
        self.items = []
        self.loading = True
        self.cursor = 0
        self.list_start = 0
        self.lines = []
        self.preview_start = 0
        self.query = ""
        self._preview_focused = False
        self.list_control = FormattedTextControl(
            self._list, focusable=True, show_cursor=False,
            key_bindings=self._kb())
        self.preview_control = FormattedTextControl(
            self._preview, focusable=True, show_cursor=False,
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
        self._right = HSplit([self.file_header,
                              Window(self.preview_control, wrap_lines=False)])
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
            out.append((name_style + (" reverse" if i == self.cursor else ""), name))
            out.append(("#ffff00" + (" reverse" if i == self.cursor else ""),
                        "  " + ",".join(map(str, hits)) + "\n"))
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
        visible = enumerate(self.lines[self.preview_start:], self.preview_start + 1)
        return [("class:search.match" if i in hitset else "class:preview.text",
                 f"{i:5} {line}\n") for i, line in visible]

    def _file_header(self):
        header_style = ("class:preview.header.focus" if self._preview_focused
                        else "class:preview.header")
        if not self.items:
            return [(header_style, " ")]
        path = self.items[self.cursor][0]
        rel = os.path.relpath(path, self.app.cwd)
        return [(header_style, " " + rel)]

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
        return kb

    def _focus_preview(self):
        self._preview_focused = True
        self._update_widths()
        self.app.application.layout.focus(self.preview_control)

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
            self.preview_start = max(0, min(len(self.lines) - 1,
                                            self.preview_start + delta))
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
