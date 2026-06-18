"""Git history view: a graph + one-line log with a per-commit preview.

Opened from the action menu ("Git: Log"). Up/Down move between commits (graph-
only lines, e.g. merges, are skipped); the preview pane shows the selected
commit's detail and diff. Enter (or the menu key) opens an action menu: check
out the commit, roll back to it, reword its message, or squash it together with
the commits after it. Reword/squash rewrite history via a non-interactive
rebase, so they require a clean working tree.
"""
import asyncio
import re

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import ScrollOffsets, Window
from prompt_toolkit.layout.dimension import Dimension

from ..util.widgets import WheelScrollControl, visible_slice
from . import git

# highlight bar painted behind the selected commit line (kept subtle so the
# log's own colours stay readable)
CURSOR_BG = "bg:#005f87"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class LogLine:
    __slots__ = ("display", "hash", "plain", "frags")

    def __init__(self, display, commit):
        self.display = display  # ANSI-coloured text (hash sentinel stripped)
        self.hash = commit      # full commit hash, or None for a graph-only line
        self.plain = _ANSI_RE.sub("", display)  # uncoloured text, for searching
        # parse the ANSI once here, not on every render — this is the bulk of
        # the per-frame cost with a long history
        self.frags = to_formatted_text(ANSI(display))


class LogView:
    def __init__(self, app):
        self.app = app
        self.lines = []
        self.cursor = 0
        self._top = 0  # first rendered row (windowing); see util.widgets
        self._search_query = ""

        self.control = WheelScrollControl(
            lambda d: self.move(d * 3),
            text=self._formatted_text,
            focusable=True,
            show_cursor=False,
            key_bindings=self._build_key_bindings(),
            get_cursor_position=lambda: Point(0, self.cursor - self._top),
        )
        self.window = Window(
            self.control,
            scroll_offsets=ScrollOffsets(top=1, bottom=1),
            always_hide_cursor=True,
            style="class:explorer.file",
            width=Dimension(min=0, preferred=0, weight=1),
        )

    # -- data -----------------------------------------------------------------
    def load(self):
        asyncio.ensure_future(self._load())

    async def _load(self):
        raw = await git.log_graph(self.app.cwd)
        lines = []
        for ln in raw.splitlines():
            if "\x00" in ln:
                pre, full, post = ln.split("\x00", 2)
                lines.append(LogLine(pre + post, full))
            else:
                lines.append(LogLine(ln, None))
        self.lines = lines
        self.cursor = next((i for i, l in enumerate(lines) if l.hash), 0)
        self.app.invalidate()

    def current(self):
        if 0 <= self.cursor < len(self.lines):
            return self.lines[self.cursor]
        return None

    def current_hash(self):
        cur = self.current()
        return cur.hash if cur else None

    # -- rendering ------------------------------------------------------------
    def _formatted_text(self):
        if not self.lines:
            return [("class:preview.dim", "  (no commits)")]
        # render only the on-screen rows (using the cached per-line fragments)
        self._top, end = visible_slice(
            self.window, len(self.lines), self.cursor, self._top)
        result = []
        for i in range(self._top, end):
            line = self.lines[i]
            frags = line.frags
            if i == self.cursor and line.hash:
                frags = [(f"{style} {CURSOR_BG}".strip(), text) for style, text in frags]
            result += frags
            if i != end - 1:
                result.append(("", "\n"))
        return result

    # -- navigation -----------------------------------------------------------
    def _commit_rows(self):
        return [i for i, l in enumerate(self.lines) if l.hash]

    def move(self, delta):
        rows = self._commit_rows()
        if not rows:
            return
        try:
            pos = rows.index(self.cursor)
        except ValueError:
            pos = 0
        self.cursor = rows[max(0, min(len(rows) - 1, pos + delta))]
        self.app.invalidate()

    def _jump(self, to_last):
        rows = self._commit_rows()
        if rows:
            self.cursor = rows[-1] if to_last else rows[0]
            self.app.invalidate()

    def refresh(self):
        self.load()

    # -- search ---------------------------------------------------------------
    def search(self):
        """Prompt for a term and jump to the next matching commit; n/N repeat.
        Matches the uncoloured log text (hash, refs, subject, date, author)."""
        self.app.open_input_dialog(
            "Search log", self._search_query, len(self._search_query),
            self._do_search)

    def _do_search(self, query):
        q = (query or "").strip()
        if not q:
            return
        self._search_query = q
        self._find(1, include_current=True)

    def _find(self, direction, include_current=False):
        q = (self._search_query or "").lower()
        rows = self._commit_rows()
        if not q or not rows:
            return
        try:
            pos = rows.index(self.cursor)
        except ValueError:
            pos = 0
        n = len(rows)
        offsets = range(0, n) if include_current else range(1, n + 1)
        for off in offsets:
            i = rows[(pos + direction * off) % n]
            if q in self.lines[i].plain.lower():
                self.cursor = i
                self.app.set_message(f"/{self._search_query}")
                self.app.invalidate()
                return
        self.app.set_message(f"no match: {self._search_query}")

    # -- action menu ----------------------------------------------------------
    def open_action_menu(self):
        h = self.current_hash()
        if not h:
            return
        self.app.open_menu(f"Commit {h[:8]}", [
            ("Checkout this commit", self.checkout),
            ("Revert to this commit", self.revert_to),
            ("Amend message (reword)", self.reword),
            ("Squash to here", self.squash),
            ("Interactive edit", self.interactive_rebase),
        ])

    def _run(self, coro, ok_msg, fail_label):
        """Await a git operation, report the outcome, then reload."""
        from .view import _git_error_summary

        async def run():
            rc, out = await coro
            if rc == 0:
                self.app.set_message(ok_msg)
            else:
                reason = _git_error_summary(out)
                self.app.set_message(f"{fail_label} failed: {reason}" if reason
                                     else f"{fail_label} failed")
            await self.app.refresh_git()
            self.load()
        asyncio.ensure_future(run())

    def checkout(self):
        h = self.current_hash()
        if not h:
            return
        self.app.confirm(
            f"Check out {h[:8]}? (detached HEAD)",
            lambda ok: self._run(git.checkout_commit(h, self.app.cwd),
                                 "checked out", "checkout") if ok else None)

    def revert_to(self):
        h = self.current_hash()
        if not h:
            return
        self.app.confirm(
            f"Revert to {h[:8]}? Commits after it and uncommitted changes "
            "will be lost.",
            lambda ok: self._run(git.reset_hard(h, self.app.cwd),
                                 "rolled back", "revert") if ok else None)

    def reword(self):
        h = self.current_hash()
        if not h:
            return

        async def start():
            if not await git.is_clean(self.app.cwd):
                self.app.set_message("commit or stash your changes first")
                return
            subject = await git.commit_subject(h, self.app.cwd)
            self.app.open_input_dialog(
                "New message", subject, len(subject),
                lambda msg: self._do_reword(h, msg))
        asyncio.ensure_future(start())

    def _do_reword(self, h, msg):
        if not msg.strip():
            self.app.set_message("reword cancelled")
            return
        self._run(git.reword(h, msg, self.app.cwd), "reworded", "reword")

    def squash(self):
        h = self.current_hash()
        if not h:
            return

        async def start():
            if not await git.is_clean(self.app.cwd):
                self.app.set_message("commit or stash your changes first")
                return
            subject = await git.commit_subject(h, self.app.cwd)
            self.app.open_input_dialog(
                "Squashed commit message", subject, len(subject),
                lambda msg: self._do_squash(h, msg))
        asyncio.ensure_future(start())

    def _do_squash(self, h, msg):
        if not msg.strip():
            self.app.set_message("squash cancelled")
            return
        self._run(git.squash_onto(h, msg, self.app.cwd), "squashed", "squash")

    def interactive_rebase(self):
        """``git rebase -i`` from the selected commit's parent, on a real
        terminal so the user edits the todo list (and resolves conflicts) in
        their own editor — unlike the scripted reword/squash above."""
        h = self.current_hash()
        if not h:
            return

        async def start():
            if not await git.is_clean(self.app.cwd):
                self.app.set_message("commit or stash your changes first")
                return
            base = await git.rebase_base(h, self.app.cwd)
            rc = await self.app.runner.run_in_term(f"git rebase -i {base}")
            self.app.set_message("rebased" if rc == 0
                                 else f"rebase exited (code {rc})")
            await self.app.refresh_git()
            self.load()
        asyncio.ensure_future(start())

    # -- keys -----------------------------------------------------------------
    def _build_key_bindings(self):
        kb = KeyBindings()

        @kb.add("j")
        @kb.add("down")
        def _(event):
            self.move(1)

        @kb.add("k")
        @kb.add("up")
        def _(event):
            self.move(-1)

        @kb.add("pagedown")
        def _(event):
            self.move(10)

        @kb.add("pageup")
        def _(event):
            self.move(-10)

        @kb.add("g")
        @kb.add("home")
        def _(event):
            self._jump(to_last=False)

        @kb.add("G")
        @kb.add("end")
        def _(event):
            self._jump(to_last=True)

        @kb.add("enter")
        def _(event):
            self.open_action_menu()

        @kb.add("/")
        def _(event):
            self.search()

        @kb.add("n")
        def _(event):
            self._find(1)

        @kb.add("N")
        def _(event):
            self._find(-1)

        # configurable action keys (a bad key spec in nshrc is skipped, as elsewhere)
        def bind(action, fn):
            key = self.app.keys.get(action)
            if not key:
                return
            try:
                kb.add(key)(lambda event: fn())
            except Exception:  # noqa: BLE001 - bad key spec; skip it
                pass

        bind("menu", self.open_action_menu)
        bind("command", lambda: self.app.switch_mode("shell"))
        bind("preview", lambda: self.app.toggle_preview())
        bind("refresh", self.refresh)
        # in the log, the quit key goes back to where it was opened from
        # (explorer / git mode) rather than quitting nsh — like Esc
        bind("quit", self.app.close_log)

        return kb
