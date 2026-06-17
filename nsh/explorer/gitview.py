"""Git mode: a flat list of the repository's changed / untracked files.

Entered with Ctrl-G. Unlike the explorer it is not a directory tree — a change
in a subdirectory shows as its full path relative to the cwd (like ``git
status``). Up/Down move, Space multi-selects, and the preview pane shows the
file's diff. There is no parent/child navigation, so the left/right keys are
inert; jumping elsewhere (e.g. via a bookmark) leaves git mode automatically.
"""
import asyncio
import os

from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import ScrollOffsets, Window
from prompt_toolkit.layout.dimension import Dimension

from .. import config
from ..util.widgets import WheelScrollControl
from . import git, model


class GitEntry:
    __slots__ = ("path", "code", "rel")

    def __init__(self, path, code, rel):
        self.path = path   # absolute Path (original case)
        self.code = code   # porcelain class: M / S / ? / C
        self.rel = rel     # display path, relative to the cwd


class GitView:
    def __init__(self, app):
        self.app = app
        self.entries = []
        self.cursor = 0
        self.selected = set()  # set[Path] of marked entries (multi-select)

        self.control = WheelScrollControl(
            lambda d: self.move(d * 3),  # mouse wheel moves the cursor
            text=self._formatted_text,
            focusable=True,
            show_cursor=False,
            key_bindings=self._build_key_bindings(),
            get_cursor_position=lambda: Point(0, self.cursor),
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
        """(Re)build the list from the app's git status, keeping cursor/selection."""
        gs = self.app.git_status
        prev = self.current()
        prev_path = prev.path if prev else None

        entries = []
        if gs and gs.is_repo:
            cwd = str(self.app.cwd)
            for abspath, code in gs.entries:
                try:
                    rel = os.path.relpath(str(abspath), cwd)
                except ValueError:  # e.g. a different drive on Windows
                    rel = str(abspath)
                entries.append(GitEntry(abspath, code, rel.replace(os.sep, "/")))
            entries.sort(key=lambda e: e.rel)
        self.entries = entries

        self.selected &= {e.path for e in entries}  # drop vanished selections
        self.cursor = 0
        if prev_path is not None:
            for i, e in enumerate(entries):
                if e.path == prev_path:
                    self.cursor = i
                    break
        if self.cursor >= len(entries):
            self.cursor = max(0, len(entries) - 1)

    def on_status_changed(self):
        """Reload when fresh git status arrives, but only while git mode is up."""
        if self.app.mode == "git":
            self.load()

    def current(self):
        if 0 <= self.cursor < len(self.entries):
            return self.entries[self.cursor]
        return None

    # -- rendering ------------------------------------------------------------
    @staticmethod
    def _cur(base, on):
        return (base + " reverse").strip() if on else base

    def _formatted_text(self):
        if not self.entries:
            return [("class:preview.dim", "  (no changes)")]
        result = []
        last = len(self.entries) - 1
        for i, e in enumerate(self.entries):
            on = i == self.cursor
            sel = e.path in self.selected
            mstyle = config.GIT_STYLE.get(e.code, "")
            symbol = config.GIT_SYMBOL.get(e.code, " ")
            name_style = "class:explorer.selected" if sel else "class:explorer.file"
            result += [
                (self._cur("class:explorer.selected" if sel else "", on),
                 "● " if sel else "  "),
                (self._cur(mstyle, on), f"{symbol} "),
                (self._cur(name_style, on), e.rel),
            ]
            if i != last:
                result.append(("", "\n"))
        return result

    # -- navigation / selection ----------------------------------------------
    def move(self, delta):
        if not self.entries:
            return
        self.cursor = max(0, min(len(self.entries) - 1, self.cursor + delta))
        self.app.invalidate()

    def toggle_select(self):
        entry = self.current()
        if entry is None:
            return
        if entry.path in self.selected:
            self.selected.discard(entry.path)
        else:
            self.selected.add(entry.path)
        self.move(1)  # toggle-and-advance
        self.app.invalidate()

    def clear_selection(self):
        if self.selected:
            self.selected.clear()
            self.app.set_message("selection cleared")
            self.app.invalidate()

    def open(self):
        entry = self.current()
        if entry is not None:
            self.app.open_file(entry.path)

    def refresh(self):
        asyncio.ensure_future(self.app.refresh_git())

    def _targets(self):
        """Paths an action acts on: the selection, else the cursor entry."""
        if self.selected:
            return [e.path for e in self.entries if e.path in self.selected]
        cur = self.current()
        return [cur.path] if cur else []

    # -- action menu (Tab) ----------------------------------------------------
    def open_action_menu(self):
        cur = self.current()
        if cur is None and not self.selected:
            return
        target = (f"{len(self.selected)} selected" if self.selected else cur.rel)
        items = []
        if self.selected:
            items.append(("Git: Stage / Unstage", self.git_stage))
            # revert is offered when the selection has tracked changes to discard
            if any(e.code != "?" for e in self.entries if e.path in self.selected):
                items.append(("Git: Revert", self.git_revert))
        elif cur.code == "?":
            if model.is_text_file(cur.path):
                items.append(("Edit", self.edit))
            items.append(("Git: Add", self.git_stage))
        else:  # M / S / C
            if model.is_text_file(cur.path):
                items.append(("Edit", self.edit))
            items.append(("Git: Stage / Unstage", self.git_stage))
            items.append(("Git: Revert", self.git_revert))
        items.append(("Git: Commit", self.app.explorer.git_commit))
        # offer push when there are commits to push (ahead of the upstream, or
        # an unpushed branch on a repo that has a remote)
        gs = self.app.git_status
        if gs and gs.can_push:
            items.append(("Git: Push", self.app.explorer.git_push))
        items.append(("Git: Branches", self.app.explorer.git_branches))
        self.app.open_menu(f"Actions · {target}", items)

    def git_stage(self):
        targets = self._targets()
        if not targets:
            return

        async def do():
            gs = self.app.git_status
            for path in targets:
                await git.stage_toggle(path, gs, self.app.cwd)
            self.selected.clear()
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    def git_revert(self):
        # only tracked changes can be reverted (untracked files have no HEAD
        # version), so drop any "?" entries from the selection/cursor
        if self.selected:
            entries = [e for e in self.entries if e.path in self.selected]
        else:
            cur = self.current()
            entries = [cur] if cur else []
        targets = [e for e in entries if e.code != "?"]
        if not targets:
            self.app.set_message("nothing to revert")
            return
        n = len(targets)
        label = (f"Revert '{targets[0].rel}'? " if n == 1
                 else f"Revert {n} files? ") + "Uncommitted changes will be lost."
        paths = [e.path for e in targets]
        self.app.confirm(label, lambda ok: self._do_revert(paths, ok))

    def _do_revert(self, paths, ok):
        if not ok:
            self.app.set_message("revert cancelled")
            return

        async def do():
            done = 0
            for path in paths:
                rc, _ = await git.revert(path, self.app.cwd)
                if rc == 0:
                    done += 1
            self.selected.clear()
            self.app.set_message(f"reverted {done}/{len(paths)} file(s)")
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    def edit(self):
        entry = self.current()
        if entry is not None:
            self.app.edit_file(entry.path)

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
            self.cursor = 0
            self.app.invalidate()

        @kb.add("G")
        @kb.add("end")
        def _(event):
            self.cursor = max(0, len(self.entries) - 1)
            self.app.invalidate()

        @kb.add("enter")
        def _(event):
            self.open()

        # There is no directory hierarchy here, so the parent/child navigation
        # keys are explicitly inert.
        @kb.add("left")
        @kb.add("right")
        @kb.add("h")
        @kb.add("l")
        @kb.add("backspace")
        def _(event):
            pass

        # Configurable action keys (remappable via the [keys] section of nshrc);
        # an invalid key spec is skipped rather than fatal, as in the explorer.
        def bind(action, fn):
            key = self.app.keys.get(action)
            if not key:
                return
            try:
                kb.add(key)(lambda event: fn())
            except Exception:  # noqa: BLE001 - bad key spec in nshrc; skip it
                pass

        bind("select", self.toggle_select)
        bind("menu", self.open_action_menu)
        bind("command", lambda: self.app.switch_mode("shell"))
        bind("bookmark", lambda: self.app.open_bookmark_menu())
        bind("preview", lambda: self.app.toggle_preview())
        bind("refresh", self.refresh)
        bind("quit", self.app.exit)

        return kb
