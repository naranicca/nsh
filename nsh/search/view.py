"""Fuzzy file-picker mode.

A query line over a ranked, scrolling result list. Typing edits the query (and
re-filters); Up/Down move the selection; Enter accepts. Entries already loaded
by the explorer are shown immediately while the full directory index is gathered
in a worker thread.
"""
import asyncio
import os

from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

from ..util.aio import run_in_thread
from ..util.width import text_width
from . import fuzzy


class SearchView:
    def __init__(self, app):
        self.app = app
        self.candidates = []
        self.results = []          # [(rel, score, positions)]
        self.cursor = 0
        self.scroll = 0
        self.loading = False
        self._index_generation = 0
        self._path_generation = 0
        self._path_task = None
        self._path_prefix = None
        self._immediate_candidates = []
        self._search_root = None
        self._search_show_hidden = False
        self._search_skip = set()
        self.remote_view = None

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
        if getattr(self.app, "_search_remote", False):
            self._start_remote(query)
            return
        self.remote_view = None
        root = self.app.cwd
        # The explorer has already scanned the current directory.  Reuse that
        # listing as an instant first-stage index while the recursive walk runs
        # in the background. Expanded descendants and the synthetic ".." row
        # are excluded: this stage deliberately represents only cwd.
        self._immediate_candidates = [
            e.name + (os.sep if e.is_dir else "")
            for e in self.app.explorer.entries
            if not e.is_parent and e.path.parent == root
        ]
        self.candidates = list(self._immediate_candidates)
        self.results = []
        self.cursor = 0
        self.scroll = 0
        self.loading = True
        self._index_generation += 1
        generation = self._index_generation
        show_hidden = self.app.explorer.show_hidden
        skip = self._exclude_dirs()
        self._search_root = root
        self._search_show_hidden = show_hidden
        self._search_skip = skip
        self._path_prefix = None
        self.query_buffer.text = query
        self.query_buffer.cursor_position = len(query)
        # Assigning the same query does not necessarily fire on_text_changed.
        self._schedule_path_lookup()
        self._refilter()
        asyncio.ensure_future(self._index(generation, root, show_hidden, skip))

    def _start_remote(self, query=""):
        self.remote_view = self.app.networkview
        index_token = self.remote_view.begin_indexing()
        self.candidates = self.remote_view.search_candidates()
        self.results = []
        self.cursor = 0
        self.scroll = 0
        self.loading = True
        self._index_generation += 1
        generation = self._index_generation
        self.query_buffer.text = query
        self.query_buffer.cursor_position = len(query)
        self._refilter()
        asyncio.ensure_future(self._index_remote(generation, index_token))

    async def _index_remote(self, generation, index_token):
        remote_view = self.remote_view
        try:
            items = await run_in_thread(
                remote_view.gather_search_candidates, index_token)
            if generation != self._index_generation:
                return
            self.candidates = items
            self.loading = False
            self._refilter()
        finally:
            remote_view.finish_indexing(index_token)
            self.app.invalidate()

    def _exclude_dirs(self):
        """The directory names to skip during search, from the nshrc
        ``search_exclude`` setting (comma/space separated). This replaces the
        built-in default list, so the user can remove a name to search it; an
        empty value skips nothing."""
        raw = self.app.settings.get("search_exclude", "") or ""
        return set(raw.replace(",", " ").split())

    async def _index(self, generation, root, show_hidden, skip):
        items = await run_in_thread(
            fuzzy.gather, root, show_hidden, skip=skip,
        )
        # A previous search may finish after the picker was reopened elsewhere;
        # never let that stale index replace the newer search's candidates.
        if generation != self._index_generation:
            return
        self.candidates = items
        self.loading = False
        self._path_generation += 1
        self._refilter()
        self.app.invalidate()

    # -- query / filtering ----------------------------------------------------
    def _on_query(self, _buffer):
        self.cursor = 0
        self.scroll = 0
        self._schedule_path_lookup()
        self._refilter()
        self.app.invalidate()

    def _schedule_path_lookup(self):
        if self.remote_view is not None or not self.loading:
            return
        query = self.query_buffer.text
        normalized = query.replace("\\", "/")
        prefix = normalized.rsplit("/", 1)[0].lower() if "/" in normalized else None
        if prefix is not None and prefix == self._path_prefix:
            return
        self._path_generation += 1
        generation = self._path_generation
        if self._path_task is not None and not self._path_task.done():
            self._path_task.cancel()
        self.candidates = list(self._immediate_candidates)
        self._path_prefix = prefix
        if prefix is None:
            self._path_task = None
            return
        self._path_task = asyncio.ensure_future(
            self._index_query_path(generation, query))

    async def _index_query_path(self, generation, query):
        items = await run_in_thread(
            fuzzy.gather_query_path, self._search_root, query,
            self._search_show_hidden, self._search_skip)
        if (generation != self._path_generation or not self.loading
                or self.remote_view is not None):
            return
        self.candidates = list(dict.fromkeys(
            [*self._immediate_candidates, *items]))
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
        if self.remote_view is not None:
            self.remote_view.open_search_result(rel)
            return
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

        try:
            width = get_app().output.get_size().columns
        except Exception:
            width = 80

        frags = []
        view = self.results[self.scroll:self.scroll + height]
        for offset, (rel, _score, positions) in enumerate(view):
            i = self.scroll + offset
            on = i == self.cursor
            # mark the selection with the ▸ arrow plus a background colour. Use
            # an explicit bg (search.selected) rather than reverse, which would
            # flip the orange match colour into a harsh yellow block.
            cur = " class:search-selected" if on else ""
            base = ("class:explorer.dir" if rel.endswith((os.sep, "/"))
                    else "class:explorer.file")
            posset = set(positions)
            frags.append((base + cur, "▸ " if on else "  "))
            for ci, ch in enumerate(rel):
                style = "class:search.match" if ci in posset else base
                frags.append((style + cur, ch))
            if on:
                # pad the selected row so its background spans the whole width
                pad = width - 2 - text_width(rel)
                if pad > 0:
                    frags.append(("class:search-selected", " " * pad))
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
