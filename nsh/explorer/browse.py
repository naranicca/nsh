"""A small centered dialog that browses the file tree of a git branch.

Opened from the branch action menu ("Browse"), it shows the branch's files in an
explorer-like list: ``Enter`` (or ``l`` / ``→``) steps into a directory, ``⌫`` /
``h`` / ``←`` steps back out, and ``y`` copies the highlighted file or directory
out of the branch into the current explorer directory (``git show`` / a recursive
``ls-tree`` extract — the working tree and checked-out branch are untouched).

It is read-only navigation over ``git ls-tree``; nothing here mutates the repo.
"""
import asyncio
from pathlib import Path

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame

from . import git
from .fileops import unique_target
from ..util import hangul
from ..util.aio import run_in_thread
from ..util.widgets import WheelScrollControl
from ..util.width import cut_to_width, text_width

WIDTH = 56    # inner list width (columns)
HEIGHT = 14   # inner list height (rows) before it scrolls


class BranchBrowser:
    def __init__(self, app):
        self.app = app
        self.active = False
        self.rev = ""          # the branch (or ref) being browsed
        self.path = ""         # current directory within the tree ("" = root)
        self.entries = []      # [(name, fullpath, is_dir)] for the current dir
        self.cursor = 0
        self.scroll = 0
        self.loading = False
        self._busy = False     # a copy is in flight

        self.control = WheelScrollControl(
            lambda d: self._move(d),
            on_click=self._on_mouse,
            text=self._text,
            focusable=True,
            show_cursor=False,
            key_bindings=self._build_kb(),
        )
        body = HSplit(
            # _text renders at most HEIGHT rows, so a content-sized window caps
            # the height there while shrinking to fit a short listing
            [Window(self.control, width=Dimension.exact(WIDTH),
                    dont_extend_height=True)],
            style="class:dialog",
            padding=0,
        )
        self.container = ConditionalContainer(
            Frame(body, title=lambda: self._title()),
            filter=Condition(lambda: self.active),
        )

    # -- lifecycle ------------------------------------------------------------
    def open(self, rev):
        """Browse branch (or ref) ``rev`` from its root."""
        self.rev = rev
        self.path = ""
        self.entries = []
        self.cursor = 0
        self.scroll = 0
        self.active = True
        self._load()

    def close(self):
        self.active = False
        self.app._dialog_closed()

    def _title(self):
        loc = f"{self.rev}:/{self.path}" if self.path else f"{self.rev}:/"
        return cut_to_width(loc, WIDTH - 2)

    # -- data -----------------------------------------------------------------
    def _load(self, select=None):
        """(Re)list the current directory of the tree, then focus ``select``."""
        self.loading = True
        self.app.invalidate()

        async def do():
            entries = await git.ls_tree(self.rev, self.path, self.app.cwd)
            self.loading = False
            self.entries = entries or []
            # cursor indexes _rows(), which prepends a ".." row off the root
            offset = 1 if self._has_parent() else 0
            self.cursor = offset  # default to the first real entry
            if select is not None:
                for i, (name, _full, _is_dir) in enumerate(self.entries):
                    if name == select:
                        self.cursor = i + offset
                        break
            self.scroll = 0
            self.app.invalidate()

        asyncio.ensure_future(do())

    # -- navigation -----------------------------------------------------------
    def _has_parent(self):
        return bool(self.path)

    def _rows(self):
        """The visible rows: a ``..`` shortcut (off the tree root) then entries."""
        rows = []
        if self._has_parent():
            rows.append(("..", None, True))
        rows.extend(self.entries)
        return rows

    def _move(self, delta):
        rows = self._rows()
        if not rows:
            return
        self.cursor = max(0, min(len(rows) - 1, self.cursor + delta))

    def _enter(self):
        """Step into the highlighted directory (``..`` goes up)."""
        rows = self._rows()
        if not rows or not (0 <= self.cursor < len(rows)):
            return
        name, full, is_dir = rows[self.cursor]
        if name == ".." and full is None:
            self._up()
        elif is_dir:
            self.path = full  # the full repo-relative path git handed us
            self._load()

    def _up(self):
        if not self.path:
            return
        parent = self.path.rsplit("/", 1)[0] if "/" in self.path else ""
        leaf = self.path.rsplit("/", 1)[-1]
        self.path = parent
        self._load(select=leaf)  # land back on the directory we came out of

    # -- copy to the explorer -------------------------------------------------
    def _copy(self):
        """Copy the highlighted file / directory out of the branch into the
        current explorer directory (never clobbering an existing name)."""
        if self._busy:
            return
        rows = self._rows()
        if not rows or not (0 <= self.cursor < len(rows)):
            return
        name, full, is_dir = rows[self.cursor]
        if name == ".." and full is None:
            return
        rev = self.rev
        dest_dir = Path(self.app.cwd)
        self._busy = True
        self.app.set_message(f"copying {name} from {rev}…")
        self.app.invalidate()

        async def do():
            try:
                if is_dir:
                    count = await self._copy_dir(rev, full, dest_dir, name)
                    msg = f"copied {name}/ ({count} file(s)) from {rev}"
                else:
                    ok = await self._copy_file(rev, full, dest_dir, name)
                    msg = (f"copied {name} from {rev}" if ok
                           else f"could not read {name} from {rev}")
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                msg = f"copy failed: {exc}"
            self._busy = False
            self.app.set_message(msg)
            # reflect the new file(s) in the explorer behind the dialog
            self.app.explorer.refresh_listing(select_name=name)
            await self.app.refresh_git()
            self.app.invalidate()

        asyncio.ensure_future(do())

    async def _copy_file(self, rev, full, dest_dir, name):
        rc, data = await git.show_bytes(rev, full, self.app.cwd)
        if rc != 0:
            return False
        target = unique_target(dest_dir, name)
        await run_in_thread(lambda: target.write_bytes(data))
        return True

    async def _copy_dir(self, rev, full, dest_dir, name):
        files = await git.ls_tree_files(rev, full, self.app.cwd)
        root = unique_target(dest_dir, name)  # the new top-level directory
        prefix = full.rstrip("/") + "/"
        count = 0
        for f in files:
            rel = f[len(prefix):] if f.startswith(prefix) else f.rsplit("/", 1)[-1]
            rc, data = await git.show_bytes(rev, f, self.app.cwd)
            if rc != 0:
                continue
            target = root / rel

            def _write(target=target, data=data):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

            await run_in_thread(_write)
            count += 1
        return count

    # -- rendering ------------------------------------------------------------
    def _text(self):
        rows = self._rows()
        total = len(rows)
        vis = HEIGHT
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + vis:
            self.scroll = self.cursor - vis + 1
        self.scroll = max(0, min(self.scroll, max(0, total - vis)))

        if self.loading:
            return [("class:dialog", _pad(" loading…", WIDTH))]
        if not rows:
            return [("class:dialog", _pad(" (empty)", WIDTH))]

        out = []
        shown = rows[self.scroll:self.scroll + vis]
        for j, (name, _full, is_dir) in enumerate(shown):
            i = self.scroll + j
            on = i == self.cursor
            # the highlight marks the cursor row; only the scroll arrows need a
            # marker column
            if j == 0 and self.scroll > 0:
                mark = "▲ "
            elif j == len(shown) - 1 and self.scroll + vis < total:
                mark = "▼ "
            else:
                mark = "  "
            icon = "▸ " if is_dir else "  "
            label = mark + icon + name + ("/" if is_dir and name != ".." else "")
            if on:
                style = "class:menu.selected"
            elif is_dir:
                style = "class:explorer.dir"
            else:
                style = "class:dialog"
            out.append((style, _pad(" " + label, WIDTH)))
            if j != len(shown) - 1:
                out.append(("", "\n"))
        return out

    def _on_mouse(self, mouse_event):
        y = mouse_event.position.y
        rows = self._rows()
        i = self.scroll + y
        if 0 <= i < len(rows):
            if i == self.cursor:
                self._enter()  # click the selected row to open it
            else:
                self.cursor = i

    # -- keys -----------------------------------------------------------------
    def _build_kb(self):
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("k")
        def _(event):
            self._move(-1)

        @kb.add("down")
        @kb.add("j")
        def _(event):
            self._move(1)

        @kb.add("pageup")
        def _(event):
            self._move(-HEIGHT)

        @kb.add("pagedown")
        def _(event):
            self._move(HEIGHT)

        @kb.add("enter")
        @kb.add("l")
        @kb.add("right")
        def _(event):
            self._enter()

        @kb.add("backspace")
        @kb.add("h")
        @kb.add("left")
        def _(event):
            self._up()

        @kb.add("y")
        def _(event):
            self._copy()

        @kb.add("escape")
        @kb.add("q")
        def _(event):
            self.close()

        hangul.add_hangul_aliases(kb)  # j/k/q/h/l/y work with the Korean IME on
        return kb


def _pad(s, width):
    return s + " " * max(0, width - text_width(s))
