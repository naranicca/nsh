"""Fuzzy file-picker mode.

A query line over a ranked, scrolling result list. Typing edits the query (and
re-filters); Up/Down move the selection; Enter accepts. The directory index is
gathered once in a worker thread so opening the picker never blocks.
"""
import asyncio
import os

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

from . import fuzzy


class SearchView:
    def __init__(self, app):
        self.app = app
        self.candidates = []
        self.results = []          # [(rel, score, positions)]
        self.cursor = 0
        self.scroll = 0
        self.loading = False

        self.query_buffer = Buffer(multiline=False, on_text_changed=self._on_query)

        self.results_control = FormattedTextControl(self._results_text, focusable=False)
        self.results_window = Window(
            self.results_control, wrap_lines=False, style="class:search.results"
        )

        self.container = HSplit(
            [
                VSplit(
                    [
                        Window(
                            FormattedTextControl(lambda: [("class:search.prompt", " search ▸ ")]),
                            width=10,
                            height=1,
                            style="class:search.prompt",
                        ),
                        Window(
                            BufferControl(self.query_buffer, key_bindings=self._kb()),
                            height=1,
                            style="class:search.input",
                        ),
                        Window(
                            FormattedTextControl(self._counter_text),
                            height=1,
                            align="right",
                            style="class:search.count",
                        ),
                    ]
                ),
                self.results_window,
            ]
        )

    # -- lifecycle ------------------------------------------------------------
    def start(self, query=""):
        self.candidates = []
        self.results = []
        self.cursor = 0
        self.scroll = 0
        self.loading = True
        self.query_buffer.text = query  # fires _on_query (harmless: no candidates yet)
        self.query_buffer.cursor_position = len(query)
        asyncio.ensure_future(self._index())

    async def _index(self):
        items = await asyncio.to_thread(
            fuzzy.gather, self.app.cwd, self.app.explorer.show_hidden
        )
        self.candidates = items
        self.loading = False
        self._refilter()
        self.app.invalidate()

    # -- query / filtering ----------------------------------------------------
    def _on_query(self, _buffer):
        self.cursor = 0
        self.scroll = 0
        self._refilter()
        self.app.invalidate()

    def _refilter(self):
        self.results = fuzzy.search(self.query_buffer.text, self.candidates)

    # -- navigation -----------------------------------------------------------
    def move(self, delta):
        if not self.results:
            return
        self.cursor = max(0, min(len(self.results) - 1, self.cursor + delta))
        self.app.invalidate()

    def accept(self):
        if not self.results:
            return
        rel = self.results[self.cursor][0]
        path = (self.app.cwd / rel).resolve()
        self.app.search_select(path)

    # -- rendering ------------------------------------------------------------
    def _counter_text(self):
        tag = " · indexing…" if self.loading else ""
        return [("class:search.count", f"{len(self.results)}/{len(self.candidates)}{tag} ")]

    def _visible_height(self):
        ri = getattr(self.results_window, "render_info", None)
        if ri is not None and ri.window_height:
            return ri.window_height
        return 20

    def _results_text(self):
        if not self.results:
            msg = "  indexing…" if self.loading else "  (no matches)"
            return [("class:preview.dim", msg)]
        height = self._visible_height()
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + height:
            self.scroll = self.cursor - height + 1

        frags = []
        view = self.results[self.scroll:self.scroll + height]
        for offset, (rel, _score, positions) in enumerate(view):
            i = self.scroll + offset
            on = i == self.cursor
            cur = " reverse" if on else ""
            base = "class:explorer.dir" if rel.endswith(os.sep) else "class:explorer.file"
            posset = set(positions)
            frags.append((base + cur, "▸ " if on else "  "))
            for ci, ch in enumerate(rel):
                style = "class:search.match" if ci in posset else base
                frags.append((style + cur, ch))
            frags.append(("", "\n"))
        return frags

    # -- key bindings (active while the query buffer is focused) --------------
    def _kb(self):
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
            self.move(-10)

        @kb.add("pagedown")
        def _(event):
            self.move(10)

        @kb.add("enter")
        def _(event):
            self.accept()

        return kb
