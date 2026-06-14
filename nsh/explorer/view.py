"""Interactive file-explorer pane.

A focusable ``FormattedTextControl`` renders the current directory; navigation
and the lazygit-style Git keys (Space/c/d) are bound on the control so they are
only active while the explorer has focus.
"""
import asyncio
from pathlib import Path

from prompt_toolkit.application.current import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import ScrollOffsets, Window
from prompt_toolkit.layout.dimension import Dimension

from .. import config
from ..util.paths import human_size, norm
from ..util.widgets import WheelScrollControl
from ..util.width import pad_to_width
from . import fileops, git, model

SIZE_COL = 8


class ExplorerView:
    def __init__(self, app):
        self.app = app
        self.entries = []
        self.cursor = 0
        self.show_hidden = False
        self.selected = set()  # set[Path] of marked entries (multi-select)
        self.clipboard = None  # ([Path, ...], "copy" | "cut")
        self._signature = ()   # snapshot used to auto-refresh on external change

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
            # preferred=0 so the VSplit ignores content width and splits the
            # space evenly with the preview instead of letting the listing
            # balloon when the preview content is narrow.
            width=Dimension(min=0, preferred=0, weight=1),
        )

    # -- data -----------------------------------------------------------------
    @staticmethod
    def _sig(entries):
        return tuple((e.name, e.is_dir, e.size, e.mtime) for e in entries)

    def load(self):
        self.entries = model.list_dir(self.app.cwd, self.show_hidden)
        self._signature = self._sig(self.entries)
        if self.cursor >= len(self.entries):
            self.cursor = max(0, len(self.entries) - 1)

    def _apply_listing(self, entries):
        """Swap in a fresh listing, keeping the cursor on the same entry."""
        cur = self.current()
        cur_name = cur.name if cur else None
        self.entries = entries
        self._signature = self._sig(entries)
        if self.selected:  # drop selections that no longer exist
            self.selected &= {e.path for e in entries}
        self.cursor = 0
        if cur_name:
            for i, e in enumerate(entries):
                if e.name == cur_name:
                    self.cursor = i
                    break
        if self.cursor >= len(entries):
            self.cursor = max(0, len(entries) - 1)

    def refresh(self):
        """Manual refresh (the 'r' key): re-list, keep the cursor, re-check git."""
        self._apply_listing(model.list_dir(self.app.cwd, self.show_hidden))
        self.app.preview.clear()
        self.app.invalidate()
        asyncio.ensure_future(self.app.refresh_git())

    def check_external_change(self):
        """Re-list only when the directory changed under us (polled)."""
        entries = model.list_dir(self.app.cwd, self.show_hidden)
        if self._sig(entries) == self._signature:
            return
        self._apply_listing(entries)
        self.app.invalidate()
        asyncio.ensure_future(self.app.refresh_git())

    def current(self):
        if 0 <= self.cursor < len(self.entries):
            return self.entries[self.cursor]
        return None

    def refresh_listing(self, select_name=None):
        """Reload the current directory, optionally moving the cursor to a name."""
        self.app.preview.clear()
        self.load()
        if select_name:
            for i, e in enumerate(self.entries):
                if e.name == select_name:
                    self.cursor = i
                    break
        self.app.invalidate()

    # -- rendering ------------------------------------------------------------
    @staticmethod
    def _cursor_style(base, on_cursor):
        return (base + " reverse").strip() if on_cursor else base

    def _formatted_text(self):
        if not self.entries:
            return [("class:explorer.file", "  (empty directory)")]
        # Derive this pane's width from the layout directly. Reading the Window's
        # render_info instead would lag one frame behind any width change (app
        # start, toggling the preview), briefly pushing the size column off-pane.
        try:
            total = get_app().output.get_size().columns
        except Exception:
            total = 80
        if self.app.show_preview and self.app._wide_enough():
            cols = total // 2  # the listing's half of the even split (it gets the larger half)
        else:
            cols = total
        # sel(2) + marker(2) + icon(2) + gap(1) + size
        name_w = max(4, cols - 7 - SIZE_COL)
        gs = self.app.git_status
        # hide the cursor-row highlight while the shell has focus: the listing
        # is still shown on top of the shell, but the active "cursor" is the
        # command line, so highlighting an explorer row would be misleading.
        cursor_shown = self.app.mode != "shell"
        result = []
        last = len(self.entries) - 1
        for i, e in enumerate(self.entries):
            on = cursor_shown and (i == self.cursor)
            sel = e.path in self.selected
            code = gs.files.get(norm(e.path)) if (gs and gs.is_repo) else None
            marker = config.GIT_SYMBOL.get(code, " ")
            mstyle = config.GIT_STYLE.get(code, "")
            estyle = "class:explorer.selected" if sel else config.entry_style(e)
            name = e.name + ("/" if e.is_dir else "")
            size = "" if e.is_dir else human_size(e.size)
            # an empty size (directories) blends into the row instead of showing
            # the grey size colour — otherwise the cursor highlight leaves a grey
            # block where the size would be.
            size_style = "class:explorer.size" if size else estyle
            result += [
                (self._cursor_style("class:explorer.selected" if sel else "", on),
                 "● " if sel else "  "),
                (self._cursor_style(mstyle, on), f"{marker} "),
                (self._cursor_style(estyle, on), f"{config.entry_icon(e)} "),
                (self._cursor_style(estyle, on), pad_to_width(name, name_w)),
                (self._cursor_style(estyle, on), " "),
                (self._cursor_style(size_style, on),
                 pad_to_width(size, SIZE_COL, align="right")),
            ]
            if i != last:
                result.append(("", "\n"))
        return result

    # -- navigation -----------------------------------------------------------
    def move(self, delta):
        if not self.entries:
            return
        self.cursor = max(0, min(len(self.entries) - 1, self.cursor + delta))
        self.app.invalidate()

    def open(self):
        entry = self.current()
        if entry is None:
            return
        if entry.is_dir:
            self.app.set_cwd(entry.path)
        else:
            self.app.open_file(entry.path)

    # -- selection ------------------------------------------------------------
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

    def toggle_hidden(self):
        self.show_hidden = not self.show_hidden
        self.app.preview.clear()
        self.load()
        self.app.invalidate()

    def _targets(self):
        """Paths a file op should act on: the selection, else the cursor entry."""
        if self.selected:
            # preserve listing order, drop anything that has since vanished
            return [e.path for e in self.entries if e.path in self.selected]
        entry = self.current()
        return [entry.path] if entry else []

    # -- file operations ------------------------------------------------------
    def copy_entry(self):
        targets = self._targets()
        if not targets:
            return
        self.clipboard = (targets, "copy")
        self.selected.clear()
        self.app.set_message(f"copied {len(targets)} item(s)  (p to paste)")
        self.app.invalidate()

    def cut_entry(self):
        targets = self._targets()
        if not targets:
            return
        self.clipboard = (targets, "cut")
        self.selected.clear()
        self.app.set_message(f"cut {len(targets)} item(s)  (p to paste)")
        self.app.invalidate()

    def paste(self):
        if not self.clipboard:
            self.app.set_message("clipboard empty")
            return
        paths, op = self.clipboard

        async def do():
            done = 0
            last = None
            for i, src in enumerate(paths, 1):
                if not src.exists():
                    continue
                try:
                    if op == "copy":
                        self.app.set_message(f"copying {i}/{len(paths)}: {src.name}…")
                        last = await fileops.copy(src, self.app.cwd)
                    else:
                        self.app.set_message(f"moving {i}/{len(paths)}: {src.name}…")
                        last = await fileops.move(src, self.app.cwd)
                    done += 1
                except Exception as exc:  # noqa: BLE001 - surfaced to the user
                    self.app.set_message(f"{src.name}: {exc}")
            if op == "cut":
                self.clipboard = None
            self.refresh_listing(select_name=last.name if last else None)
            verb = "copied" if op == "copy" else "moved"
            self.app.set_message(f"{verb} {done}/{len(paths)} item(s)")
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    def delete_entry(self):
        targets = self._targets()
        if not targets:
            return
        if len(targets) == 1:
            label = f"Delete '{targets[0].name}'? This cannot be undone."
        else:
            label = f"Delete {len(targets)} items? This cannot be undone."
        self.app.confirm(label, lambda ok: self._do_delete(targets, ok))

    def _do_delete(self, targets, ok):
        if not ok:
            self.app.set_message("delete cancelled")
            return

        async def do():
            done = 0
            for path in targets:
                try:
                    await fileops.delete(path)
                    done += 1
                except Exception as exc:  # noqa: BLE001
                    self.app.set_message(f"{path.name}: {exc}")
            self.selected.clear()
            self.refresh_listing()
            self.app.set_message(f"deleted {done} item(s)")
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    def rename_entry(self):
        entry = self.current()
        if entry is None:
            return
        # cursor at the end of the name minus its extension
        stem_len = len(Path(entry.name).stem)
        self.app.open_input_dialog(
            "Rename", entry.name, stem_len,
            lambda name: self._do_rename(entry, name),
        )

    def _do_rename(self, entry, name):
        name = name.strip()
        if not name or name == entry.name:
            self.app.set_message("rename cancelled")
            return
        try:
            target = fileops.rename(entry.path, name)
            self.refresh_listing(select_name=target.name)
            self.app.set_message(f"renamed to: {target.name}")
            asyncio.ensure_future(self.app.refresh_git())
        except Exception as exc:  # noqa: BLE001
            self.app.set_message(f"rename failed: {exc}")

    def new_dir(self):
        self.app.open_input_dialog("New folder", "", 0, self._do_new_dir)

    def _do_new_dir(self, name):
        name = name.strip()
        if not name:
            self.app.set_message("cancelled")
            return
        try:
            target = fileops.make_dir(self.app.cwd, name)
            self.refresh_listing(select_name=target.name)
            self.app.set_message(f"created folder: {target.name}")
        except Exception as exc:  # noqa: BLE001
            self.app.set_message(f"mkdir failed: {exc}")

    def new_file(self):
        self.app.open_input_dialog("New file", "", 0, self._do_new_file)

    def _do_new_file(self, name):
        name = name.strip()
        if not name:
            self.app.set_message("cancelled")
            return
        try:
            target = fileops.make_file(self.app.cwd, name)
            self.refresh_listing(select_name=target.name)
            self.app.set_message(f"created file: {target.name}")
            asyncio.ensure_future(self.app.refresh_git())
        except Exception as exc:  # noqa: BLE001
            self.app.set_message(f"touch failed: {exc}")

    def edit_entry(self):
        entry = self.current()
        if entry is None or entry.is_dir:
            return
        self.app.edit_file(entry.path)

    # -- action menu (Tab) ----------------------------------------------------
    def open_command_menu(self):
        if not self.selected and self.current() is None:
            return
        target = (f"{len(self.selected)} selected" if self.selected
                  else self.current().name)
        cur = self.current()
        items = []
        # "Edit" only for a single text file under the cursor (not a directory,
        # image, or binary), and not while multi-selecting.
        if (not self.selected and cur is not None and not cur.is_dir
                and not cur.is_image and model.is_text_file(cur.path)):
            items.append(("Edit", self.edit_entry))
        items += [
            ("Copy", self.copy_entry),
            ("Cut", self.cut_entry),
        ]
        if self.clipboard:
            items.append(("Paste", self.paste))
        items += [
            ("Rename", self.rename_entry),
            ("Delete", self.delete_entry),
            ("New folder", self.new_dir),
            ("New file", self.new_file),
        ]
        if self.app.git_status and self.app.git_status.is_repo:
            items += [
                ("Git: stage / unstage", self.git_stage),
                ("Git: commit", self.git_commit),
                ("Git: diff", self.git_diff),
                ("Git: Branches", self.git_branches),
            ]
        self.app.open_menu(f"Actions · {target}", items)

    # -- help (?) -------------------------------------------------------------
    _NAV_HELP = [
        ("↑ ↓  k j", "move cursor"),
        ("↵  l  →", "open file / enter directory"),
        ("⌫  h  ←", "parent directory"),
        ("g  G", "top / bottom"),
        ("PgUp PgDn", "page up / down"),
    ]
    _ACTION_HELP = [
        ("select", "select / deselect (multi-select)"),
        ("menu", "action menu (copy, rename, git…)"),
        ("copy", "copy"), ("cut", "cut"), ("paste", "paste"),
        ("rename", "rename"), ("new_dir", "new folder"), ("new_file", "new file"),
        ("delete", "delete"), ("bookmark", "bookmarks"), ("find", "fuzzy find"),
        ("command", "command-line mode"), ("preview", "toggle preview pane"),
        ("hidden", "toggle hidden files"), ("refresh", "refresh"),
        ("help", "this help"), ("quit", "quit"),
    ]

    @staticmethod
    def _key_label(key):
        return {" ": "Space", "tab": "Tab", "escape": "Esc",
                "enter": "Enter"}.get(key, key)

    def show_help(self):
        def line(keys, desc):
            return (f"{keys:<12}{desc}", None)
        items = [line(k, d) for k, d in self._NAV_HELP]
        for action, desc in self._ACTION_HELP:
            key = self.app.keys.get(action)
            if key:
                items.append(line(self._key_label(key), desc))
        self.app.open_menu("Keys", items)

    # -- git actions (lazygit-style) ------------------------------------------
    def _require_repo(self):
        if not (self.app.git_status and self.app.git_status.is_repo):
            self.app.set_message("not a git repository")
            return False
        return True

    def git_stage(self):
        entry = self.current()
        if entry is None or not self._require_repo():
            return

        async def do():
            await git.stage_toggle(entry.path, self.app.git_status, self.app.cwd)
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    def git_commit(self):
        if not self._require_repo():
            return
        self.app.open_input_dialog("Commit message", "", 0, self._do_commit)

    def _do_commit(self, message):
        if not message.strip():
            self.app.set_message("commit cancelled")
            return

        async def do():
            rc, out = await git.commit(message, self.app.cwd)
            self.app.shell.append(out.strip() or "committed",
                                  "class:shell.output" if rc == 0 else "class:shell.error")
            await self.app.refresh_git()
            self.app.set_message("committed" if rc == 0 else "commit failed")
        asyncio.ensure_future(do())

    def git_diff(self):
        entry = self.current()
        if entry is None or not self._require_repo():
            return

        async def do():
            text = await git.diff(entry.path, self.app.cwd)
            self.app.shell.append(f"--- diff: {entry.name} ---", "class:shell.command")
            if not text:
                self.app.shell.append("(no changes)")
            for line in text.splitlines():
                if line.startswith("+"):
                    style = "class:git.staged"
                elif line.startswith("-"):
                    style = "class:git.untracked"
                elif line.startswith("@@"):
                    style = "class:shell.command"
                else:
                    style = "class:shell.output"
                self.app.shell.append(line, style)
            self.app.switch_mode("shell")
        asyncio.ensure_future(do())

    def git_new_branch(self):
        if not self._require_repo():
            return
        self.app.open_input_dialog("New branch name", "", 0, self._do_new_branch)

    def _do_new_branch(self, name):
        name = name.strip()
        if not name:
            self.app.set_message("cancelled")
            return

        async def do():
            rc, out = await git.create_branch(name, self.app.cwd)
            if rc == 0:
                self.refresh()  # branch switch may change the working tree
                self.app.set_message(f"switched to new branch: {name}")
            else:
                self.app.set_message(f"branch failed: {out.strip()}")
        asyncio.ensure_future(do())

    def git_branches(self):
        if not self._require_repo():
            return

        async def do():
            branches, cur = await git.list_branches(self.app.cwd)
            items = [("+ New Branch", self.git_new_branch)]
            for b in branches:
                mark = "● " if b == cur else "  "
                items.append((f"{mark}{b}", lambda b=b: self._do_checkout(b)))
            self.app.open_menu("Branches", items)
        asyncio.ensure_future(do())

    def _do_checkout(self, name):
        async def do():
            rc, out = await git.checkout_branch(name, self.app.cwd)
            if rc == 0:
                self.refresh()  # the new branch may have a different working tree
                self.app.set_message(f"checked out: {name}")
            else:
                self.app.set_message(f"checkout failed: {out.strip()}")
        asyncio.ensure_future(do())

    # -- key bindings ---------------------------------------------------------
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
        @kb.add("l")
        @kb.add("right")
        def _(event):
            self.open()

        @kb.add("h")
        @kb.add("left")
        @kb.add("backspace")
        def _(event):
            # going up: land the cursor on the directory we're leaving
            self.app.set_cwd(self.app.cwd.parent, select_name=self.app.cwd.name)

        # Configurable action keys (remappable via the [keys] section of nshrc).
        actions = {
            "copy": self.copy_entry,
            "cut": self.cut_entry,
            "paste": self.paste,
            "delete": self.delete_entry,
            "rename": self.rename_entry,
            "new_dir": self.new_dir,
            "new_file": self.new_file,
            "select": self.toggle_select,
            "menu": self.open_command_menu,
            "bookmark": lambda: self.app.open_bookmark_menu(),
            "find": lambda: self.app.enter_search(),
            "command": lambda: self.app.switch_mode("shell"),
            "preview": lambda: self.app.toggle_preview(),
            "hidden": self.toggle_hidden,
            "refresh": self.refresh,
            "help": self.show_help,
            "quit": self.app.exit,
        }
        for action, handler in actions.items():
            key = self.app.keys.get(action)
            if not key:
                continue
            try:
                kb.add(key)(self._action_handler(handler))
            except Exception:  # noqa: BLE001 - invalid key spec in nshrc; skip it
                pass

        return kb

    @staticmethod
    def _action_handler(func):
        def handler(event):
            func()
        return handler
