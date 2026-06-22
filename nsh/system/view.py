"""Process-manager mode (a small task manager).

A header shows overall CPU / memory / disk usage; below it a scrolling, cursor-
selectable list of the running processes. The list can be sorted by CPU% or
MEM%, and the selected process can be killed (with a confirm). The data is
sampled in a worker thread on a timer so the UI stays responsive.
"""
import asyncio

from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.margins import Margin
from prompt_toolkit.mouse_events import MouseEventType

from ..util.aio import run_in_thread
from ..util.paths import human_size
from ..util.widgets import WheelScrollControl
from ..util.width import cut_to_width, pad_to_width
from . import sysinfo

REFRESH = 2.0          # seconds between samples
HEADER_HEIGHT = 4      # cpu + mem + disk + column header
W_PID, W_CPU, W_MEM, W_RSS = 7, 6, 6, 10


class _ListScrollbar(Margin):
    """Scrollbar for the process list (it windows its own content, so the
    built-in margin can't see the full length)."""

    def __init__(self, view):
        self.view = view

    def get_width(self, get_ui_content):
        return 1 if self.view._visible_count() > self.view._visible_height() else 0

    def create_margin(self, window_render_info, width, height):
        total = self.view._visible_count()
        if height <= 0 or total <= height:
            return []
        scroll = max(0, min(self.view._top, total - height))
        thumb = max(1, min(height, height * height // total))
        top = (height - thumb) * scroll // max(1, total - height)
        top = max(0, min(top, height - thumb))
        frags = []
        for row in range(height):
            inside = top <= row < top + thumb
            frags.append(("class:scrollbar.button" if inside
                          else "class:scrollbar.background", " "))
            if row < height - 1:
                frags.append(("", "\n"))
        return frags


class SystemView:
    def __init__(self, app):
        self.app = app
        self.monitor = sysinfo.Monitor()
        self.snapshot = None
        self.procs = []          # sorted process list (sysinfo.Proc)
        self.cursor = 0
        self._top = 0            # first rendered row (windowing)
        self.sort = "cpu"        # "cpu" | "mem"
        self._searching = False  # the search bar is open / filtering
        self._task = None

        self.header_control = FormattedTextControl(self._header_text)
        self.search_buffer = Buffer(multiline=False, on_text_changed=self._on_search)
        self.search_control = BufferControl(
            self.search_buffer, key_bindings=self._search_kb())
        self.list_control = WheelScrollControl(
            lambda d: self.move(d * 3),  # wheel moves the cursor
            on_click=self._on_mouse,     # click selects the process row
            text=self._list_text, focusable=True, show_cursor=False,
            key_bindings=self._kb())
        self.list_window = Window(
            self.list_control, style="class:explorer.file",
            right_margins=[_ListScrollbar(self)])

        self.search_bar = ConditionalContainer(
            VSplit([
                Window(FormattedTextControl(
                    lambda: [("class:system.label", " search ▸ ")]),
                    width=9, height=1, style="class:system.label"),
                Window(self.search_control, height=1, style="class:notes.input"),
            ]),
            filter=Condition(lambda: self._searching),
        )

        self.container = HSplit([
            Window(self.header_control, height=HEADER_HEIGHT, style="class:system.header"),
            Window(height=1, char="─", style="class:preview.border"),
            self.search_bar,
            self.list_window,
        ])

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        self.cursor = 0
        self._top = 0
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())

    async def _loop(self):
        # refresh on a timer for as long as we're in system mode
        from ..app import SYSTEM
        while self.app.mode == SYSTEM:
            await self.refresh()
            await asyncio.sleep(REFRESH)

    async def refresh(self):
        pid = self._selected_pid()
        snap = await run_in_thread(self.monitor.snapshot)
        self.snapshot = snap
        self._apply_sort()
        self._restore_cursor(pid)
        self.app.invalidate()

    def _apply_sort(self):
        procs = list(self.snapshot.procs) if self.snapshot else []
        if self.sort == "name":
            procs.sort(key=lambda p: p.name.lower())  # ascending A→Z
        else:
            key = (lambda p: p.cpu) if self.sort == "cpu" else (lambda p: p.mem)
            procs.sort(key=key, reverse=True)          # highest first
        self.procs = procs

    def set_sort(self, mode):
        if mode != self.sort:
            pid = self._selected_pid()
            self.sort = mode
            self._apply_sort()
            self._restore_cursor(pid)
            self.app.invalidate()

    # -- search / filtering ---------------------------------------------------
    def _query(self):
        return self.search_buffer.text.strip().lower() if self._searching else ""

    def _visible(self):
        """The process list after applying the name/PID filter."""
        q = self._query()
        if not q:
            return self.procs
        return [p for p in self.procs
                if q in (p.cmd or p.name).lower() or q in str(p.pid)]

    def _visible_count(self):
        return len(self._visible())

    def _selected_pid(self):
        vis = self._visible()
        return vis[self.cursor].pid if 0 <= self.cursor < len(vis) else None

    def _restore_cursor(self, pid):
        vis = self._visible()
        if pid is not None:
            for i, p in enumerate(vis):
                if p.pid == pid:
                    self.cursor = i
                    return
        self.cursor = max(0, min(self.cursor, len(vis) - 1))

    def _on_search(self, _buffer):
        self.cursor = 0
        self._top = 0
        self.app.invalidate()

    def start_search(self):
        self._searching = True
        self.search_buffer.reset()
        self.cursor = 0
        self._top = 0
        self.app.application.layout.focus(self.search_control)
        self.app.invalidate()

    def cancel_search(self):
        self._searching = False
        self.search_buffer.reset()
        self.cursor = 0
        self._top = 0
        self.app.application.layout.focus(self.list_control)
        self.app.invalidate()

    def current(self):
        vis = self._visible()
        if 0 <= self.cursor < len(vis):
            return vis[self.cursor]
        return None

    # -- navigation -----------------------------------------------------------
    def move(self, delta):
        n = self._visible_count()
        if n == 0:
            return
        self.cursor = max(0, min(n - 1, self.cursor + delta))
        self.app.invalidate()

    def _on_mouse(self, mouse_event):
        """Click selects the process row under the pointer."""
        if self.app.consume_menu_click():
            return
        idx = self._top + mouse_event.position.y
        if not (0 <= idx < self._visible_count()):
            return
        self.app.application.layout.focus(self.list_control)
        self.cursor = idx
        self.app.invalidate()

    def _visible_height(self):
        ri = getattr(self.list_window, "render_info", None)
        if ri is not None and ri.window_height:
            return ri.window_height
        return 20

    # -- kill -----------------------------------------------------------------
    def kill_selected(self, force=False):
        proc = self.current()
        if proc is None:
            return
        what = "Force-kill" if force else "Terminate"
        self.app.confirm(
            f"{what} '{proc.name}' (PID {proc.pid})?",
            lambda ok: self._do_kill(proc, force) if ok else None)

    def _do_kill(self, proc, force):
        ok, err = sysinfo.kill(proc.pid, force)
        if ok:
            self.app.set_message(f"killed {proc.name} (PID {proc.pid})")
        else:
            self.app.set_message(f"kill failed: {err}")
        asyncio.ensure_future(self.refresh())

    # -- rendering ------------------------------------------------------------
    @staticmethod
    def _bar(pct, width=14):
        if pct is None:
            return "[" + " " * width + "] n/a"
        pct = max(0.0, min(100.0, pct))
        filled = int(round(pct / 100 * width))
        return "[" + "█" * filled + "░" * (width - filled) + f"] {pct:3.0f}%"

    def _term_cols(self):
        try:
            return get_app().output.get_size().columns
        except Exception:
            return 80

    def _header_text(self):
        s = self.snapshot
        cpu = s.cpu if s else None
        if s and s.mem_total:
            mem_pct = 100.0 * (s.mem_used or 0) / s.mem_total
            mem_extra = f"   {human_size(s.mem_used or 0)} / {human_size(s.mem_total)}"
        else:
            mem_pct, mem_extra = None, ""
        if s and s.disk_total:
            disk_used = s.disk_used or 0
            disk_pct = 100.0 * disk_used / s.disk_total
            disk_free = max(0, s.disk_total - disk_used)
            disk_extra = (f"   {human_size(disk_used)} / {human_size(s.disk_total)}"
                          f"  ({human_size(disk_free)} free)")
        else:
            disk_pct, disk_extra = None, ""
        out = [
            ("class:system.label", " CPU  "),
            ("class:system.bar", self._bar(cpu)),
            ("", "\n"),
            ("class:system.label", " MEM  "),
            ("class:system.bar", self._bar(mem_pct)),
            ("class:system.dim", mem_extra),
            ("", "\n"),
            ("class:system.label", " DISK "),
            ("class:system.bar", self._bar(disk_pct)),
            ("class:system.dim", disk_extra),
            ("", "\n"),
        ]
        out += self._column_header()
        return out

    def _sort_click(self, mode):
        """A column-header mouse handler that sorts by ``mode`` on a click."""
        def handler(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                self.set_sort(mode)
            return None
        return handler

    def _column_header(self):
        # ▼ on a descending column (cpu/mem, highest first), ▲ on the ascending
        # name column — so the marker also hints the sort direction. The CPU% /
        # MEM% / PROC headers are clickable to sort by that column.
        def col(label, key, w):
            arrow = " ▼" if self.sort == key else ""
            text = (label + arrow).rjust(w)
            style = "class:system.sortcol" if self.sort == key else "class:system.colhead"
            return (style, text, self._sort_click(key))
        cols = self._term_cols()
        name_w = max(4, cols - (W_PID + W_CPU + W_MEM + W_RSS + 5))
        ch = "class:system.colhead"
        name_on = self.sort == "name"
        proc_label = (f" PROC ▲ ({self._visible_count()})" if name_on
                      else f" PROC  ({self._visible_count()})")
        return [
            (ch, " "),
            (ch, "PID".rjust(W_PID)),
            (ch, " "), col("CPU%", "cpu", W_CPU),
            (ch, " "), col("MEM%", "mem", W_MEM),
            (ch, " "),
            (ch, "RSS".rjust(W_RSS)),
            (ch, " "),
            ("class:system.sortcol" if name_on else ch,
             pad_to_width(proc_label, name_w), self._sort_click("name")),
        ]

    def _list_text(self):
        procs = self._visible()
        if not procs:
            if self.snapshot is None:
                return [("class:preview.dim", "  sampling…")]
            return [("class:preview.dim",
                     "  (no matches)" if self._searching else "  (no processes)")]
        cols = self._term_cols()
        height = self._visible_height()
        sb = 1 if len(procs) > height else 0
        name_w = max(4, cols - (W_PID + W_CPU + W_MEM + W_RSS + 5) - sb)

        # window the visible slice around the cursor
        if self.cursor < self._top:
            self._top = self.cursor
        elif self.cursor >= self._top + height:
            self._top = self.cursor - height + 1
        self._top = max(0, min(self._top, max(0, len(procs) - height)))

        frags = []
        end = min(len(procs), self._top + height)
        for i in range(self._top, end):
            p = procs[i]
            on = i == self.cursor
            row = (f" {p.pid:>{W_PID}} {p.cpu:>{W_CPU}.1f} {p.mem:>{W_MEM}.1f} "
                   f"{human_size(p.rss):>{W_RSS}} ")
            name = cut_to_width(p.cmd or p.name, name_w)
            cell = row + name
            base = "class:system.row.sel" if on else "class:system.row"
            frags.append((base, pad_to_width(cell, cols - sb)))
            if i != end - 1:
                frags.append(("", "\n"))
        return frags

    # -- key bindings ---------------------------------------------------------
    def _kb(self):
        kb = KeyBindings()

        @kb.add("down")
        @kb.add("j")
        def _(event):
            self.move(1)

        @kb.add("up")
        @kb.add("k")
        def _(event):
            if self.cursor <= 0 and self._searching:
                self.app.application.layout.focus(self.search_control)
                self.app.invalidate()
            else:
                self.move(-1)

        @kb.add("pagedown")
        def _(event):
            self.move(self._visible_height())

        @kb.add("pageup")
        def _(event):
            self.move(-self._visible_height())

        @kb.add("g")
        @kb.add("home")
        def _(event):
            self.cursor = 0
            self.app.invalidate()

        @kb.add("G")
        @kb.add("end")
        def _(event):
            self.cursor = max(0, self._visible_count() - 1)
            self.app.invalidate()

        @kb.add("c")
        def _(event):
            self.set_sort("cpu")

        @kb.add("m")
        def _(event):
            self.set_sort("mem")

        @kb.add("n")
        def _(event):
            self.set_sort("name")

        @kb.add("/")
        def _(event):
            self.start_search()

        # Esc clears an active filter; with none it falls through to the global
        # Esc (leave system mode).
        @kb.add("escape", filter=Condition(lambda: self._searching))
        def _(event):
            self.cancel_search()

        @kb.add("x")
        @kb.add("delete")
        def _(event):
            self.kill_selected(force=False)

        @kb.add("K")
        def _(event):
            self.kill_selected(force=True)

        @kb.add("r")
        def _(event):
            asyncio.ensure_future(self.refresh())

        return kb

    def _search_kb(self):
        kb = KeyBindings()

        @kb.add("enter")
        @kb.add("down")
        def _(event):
            if self._visible_count():
                self.app.application.layout.focus(self.list_control)
                self.app.invalidate()

        @kb.add("escape")
        @kb.add("c-c")
        def _(event):
            self.cancel_search()

        return kb
