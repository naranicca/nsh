"""Interactive file-explorer pane.

A focusable ``FormattedTextControl`` renders the current directory; navigation
and the lazygit-style Git keys (Space/c/d) are bound on the control so they are
only active while the explorer has focus.
"""
import asyncio
from pathlib import Path

from prompt_toolkit.application.current import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import ScrollOffsets, Window
from prompt_toolkit.layout.dimension import Dimension

from .. import config
from ..util.paths import human_size, norm
from ..util.widgets import WheelScrollControl
from ..util.width import pad_to_width
from . import fileops, git, model

SIZE_COL = 8


def _git_error_summary(output):
    """A concise one-line reason from a failed git command's output, for the
    status bar (the full text still goes to the shell scrollback). Prefers a
    ``fatal:``/``error:`` line, else the last line; trims the noisy prefix."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if not lines:
        return ""
    reason = next((ln for ln in lines if ln.startswith(("fatal:", "error:"))), lines[-1])
    for prefix in ("fatal: ", "error: "):
        if reason.startswith(prefix):
            reason = reason[len(prefix):]
            break
    return reason[:120]


class ExplorerView:
    def __init__(self, app):
        self.app = app
        self.entries = []
        self.cursor = 0
        self.show_hidden = False
        self.selected = set()  # set[Path] of marked entries (multi-select)
        self.expanded = set()  # set[Path] of directories expanded inline (tree)
        self.clipboard = None  # ([Path, ...], "copy" | "cut")
        self._signature = ()   # snapshot used to auto-refresh on external change
        # inline rename state: edits the cursor row's name in place (no dialog)
        self._renaming = False
        self._rename_entry = None
        self._rename_text = ""
        self._rename_pos = 0   # cursor index within _rename_text

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

    def _list(self):
        """The cwd listing flattened into a tree: each expanded directory's
        contents follow it, indented one level deeper."""
        return self._flatten(self.app.cwd, 0)

    def _flatten(self, directory, depth):
        out = []
        for e in model.list_dir(directory, self.show_hidden):
            e.depth = depth
            out.append(e)
            # recurse into expanded directories (not symlinks — avoid cycles)
            if e.is_dir and not e.is_link and e.path in self.expanded:
                out.extend(self._flatten(e.path, depth + 1))
        return out

    def load(self):
        self.entries = self._list()
        self._signature = self._sig(self.entries)
        if self.cursor >= len(self.entries):
            self.cursor = max(0, len(self.entries) - 1)

    def _apply_listing(self, entries):
        """Swap in a fresh listing, keeping the cursor on the same entry."""
        cur = self.current()
        cur_path = cur.path if cur else None
        self.entries = entries
        self._signature = self._sig(entries)
        if self.selected:  # drop selections that no longer exist
            self.selected &= {e.path for e in entries}
        self.cursor = 0
        if cur_path is not None:
            for i, e in enumerate(entries):
                if e.path == cur_path:
                    self.cursor = i
                    break
        if self.cursor >= len(entries):
            self.cursor = max(0, len(entries) - 1)

    def refresh(self):
        """Manual refresh (the 'r' key): re-list, keep the cursor, re-check git."""
        self._apply_listing(self._list())
        self.app.preview.clear()
        self.app.invalidate()
        asyncio.ensure_future(self.app.refresh_git())

    def check_external_change(self):
        """Re-list only when the directory changed under us (polled)."""
        entries = self._list()
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
            # on the cursor row the size uses the row (name) style so the "reverse"
            # highlight stays one solid colour instead of a darker grey block at
            # the right edge; elsewhere the size keeps its grey (directories, which
            # have no size, already fall back to the row style).
            size_style = estyle if on else ("class:explorer.size" if size else estyle)
            # tree indent (2 cells per level) sits before the icon; the name cell
            # shrinks to match so the size column stays put.
            indent = "  " * e.depth
            nw = max(4, name_w - len(indent))
            # expanded directories get a down-pointing caret instead of ▸
            icon = "▾" if (e.is_dir and e.path in self.expanded) else config.entry_icon(e)
            # the row being renamed shows an editable name cell instead of the name
            if self._renaming and on:
                name_frags = self._rename_name_fragments(nw)
            else:
                name_frags = [(self._cursor_style(estyle, on), pad_to_width(name, nw))]
            result += [
                (self._cursor_style("class:explorer.selected" if sel else "", on),
                 "● " if sel else "  "),
                # the trailing gap uses the row style, not the mark's — otherwise
                # the cursor-row "reverse" paints the mark colour one cell too far
                (self._cursor_style(mstyle, on), marker),
                (self._cursor_style(estyle, on), " " + indent),
                (self._cursor_style(estyle, on), f"{icon} "),
                *name_frags,
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

    def expand_or_open(self):
        """Right arrow (when right_expand is on): fold/unfold a real directory,
        but open a file or symlinked dir, so the key is never a dead end."""
        entry = self.current()
        if entry is not None and entry.is_dir and not entry.is_link:
            self.toggle_expand()
        else:
            self.open()

    def toggle_expand(self):
        """Expand/collapse the directory under the cursor inline (tree view)."""
        entry = self.current()
        if entry is None or not entry.is_dir or entry.is_link:
            return
        if entry.path in self.expanded:
            self.expanded.discard(entry.path)
        else:
            self.expanded.add(entry.path)
        # rebuild the flattened listing, keeping the cursor on this directory
        self._apply_listing(self._list())
        self.app.preview.clear()
        self.app.invalidate()

    def collapse_or_up(self):
        """Left/h/backspace: fold the tree where possible, else go up a level.

        - on an expanded directory  -> collapse it
        - inside an expanded subtree -> collapse its parent and move there
        - otherwise (top level)      -> change to the parent directory
        """
        entry = self.current()
        if entry is not None and entry.is_dir and entry.path in self.expanded:
            self.expanded.discard(entry.path)
            self._apply_listing(self._list())  # cursor stays on this directory
            self.app.preview.clear()
            self.app.invalidate()
            return
        if entry is not None and entry.depth > 0:
            parent = entry.path.parent
            self.expanded.discard(parent)
            self._apply_listing(self._list())  # the child is gone now
            for i, e in enumerate(self.entries):
                if e.path == parent:
                    self.cursor = i
                    break
            self.app.preview.clear()
            self.app.invalidate()
            return
        # going up: land the cursor on the directory we're leaving
        self.app.set_cwd(self.app.cwd.parent, select_name=self.app.cwd.name)

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
        """Begin editing the cursor row's name in place (no dialog)."""
        entry = self.current()
        if entry is None:
            return
        self._rename_entry = entry
        self._rename_text = entry.name
        self._rename_pos = len(Path(entry.name).stem)  # before the extension
        self._renaming = True
        self.app.invalidate()

    # -- inline rename editing ------------------------------------------------
    def _rename_insert(self, text):
        p = self._rename_pos
        self._rename_text = self._rename_text[:p] + text + self._rename_text[p:]
        self._rename_pos += len(text)
        self.app.invalidate()

    def _rename_backspace(self):
        if self._rename_pos > 0:
            p = self._rename_pos
            self._rename_text = self._rename_text[:p - 1] + self._rename_text[p:]
            self._rename_pos -= 1
            self.app.invalidate()

    def _rename_delete(self):
        p = self._rename_pos
        if p < len(self._rename_text):
            self._rename_text = self._rename_text[:p] + self._rename_text[p + 1:]
            self.app.invalidate()

    def _rename_move(self, delta):
        self._rename_pos = max(0, min(len(self._rename_text), self._rename_pos + delta))
        self.app.invalidate()

    def _rename_set_pos(self, pos):
        self._rename_pos = max(0, min(len(self._rename_text), pos))
        self.app.invalidate()

    def _rename_cancel(self):
        self._renaming = False
        self._rename_entry = None
        self.app.set_message("rename cancelled")
        self.app.invalidate()

    def _rename_commit(self):
        entry = self._rename_entry
        name = self._rename_text.strip()
        self._renaming = False
        self._rename_entry = None
        if entry is None or not name or name == entry.name:
            self.app.set_message("rename cancelled")
            self.app.invalidate()
            return
        try:
            target = fileops.rename(entry.path, name)
            self.refresh_listing(select_name=target.name)
            self.app.set_message(f"renamed to: {target.name}")
            asyncio.ensure_future(self.app.refresh_git())
        except Exception as exc:  # noqa: BLE001
            self.app.set_message(f"rename failed: {exc}")
        self.app.invalidate()

    def _rename_name_fragments(self, name_w):
        """Render the edited name as fragments exactly ``name_w`` cells wide,
        horizontally scrolled so the block cursor stays visible."""
        text, pos = self._rename_text, self._rename_pos
        # keep the cursor within the visible window of width name_w
        start = pos - (name_w - 1) if pos > name_w - 1 else 0
        view = text[start:start + name_w]
        cpos = pos - start
        before = view[:cpos]
        at = view[cpos] if cpos < len(view) else " "
        after = view[cpos + 1:] if cpos < len(view) else ""
        pad = " " * max(0, name_w - (len(before) + 1 + len(after)))
        return [
            ("class:explorer.rename", before),
            ("class:explorer.rename.cursor", at),
            ("class:explorer.rename", after + pad),
        ]

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
            gs = self.app.git_status
            code = gs.files.get(norm(cur.path)) if cur else None
            if code == "?":
                # untracked file: stage/unstage, commit and diff don't apply
                # yet, so only offer to start tracking it.
                items.append(("Git: Add", self.git_stage))
            elif code in ("M", "S", "C"):
                # has changes (modified / staged / conflicted): full set
                items += [
                    ("Git: Stage / Unstage", self.git_stage),
                    ("Git: Commit", self.git_commit),
                    ("Git: Diff", self.git_diff),
                    ("Git: Revert", self.git_revert),
                ]
            # clean tracked file (code is None): nothing to stage/commit/diff
            items.append(("Git: Branches", self.git_branches))
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
        # capture the commit target now (the modal dialog can't change it):
        # the selected files, or '.' (the whole cwd) when nothing is selected
        sel = self.app.active_selection()
        paths = [str(p) for p in sel] if sel else ["."]
        self.app.open_input_dialog(
            "Commit message", "", 0, lambda msg: self._do_commit(msg, paths))

    def _do_commit(self, message, paths):
        if not message.strip():
            self.app.set_message("commit cancelled")
            return

        async def do():
            # commit by pathspec, like the original nsh (`git commit <files|.>`):
            # selected files, else '.'. Explicitly selected files are staged
            # first so an untracked selection commits too; '.' is left as-is so
            # it doesn't sweep in untracked files.
            if paths != ["."]:
                await git.add_paths(paths, self.app.cwd)
            rc, out = await git.commit(message, self.app.cwd, paths)
            if rc == 0:
                self.app.shell.append(out.strip() or "committed", "class:shell.output")
                self.app.set_message("committed")
                sel = self.app.active_selection()  # consumed: clear the marks
                if sel:
                    sel.clear()
            else:
                # surface the real reason (identity unset, nothing to commit,
                # hook…) in the status bar; full output goes to the scrollback
                self.app.shell.append(out.strip() or "git commit failed",
                                      "class:shell.error")
                reason = _git_error_summary(out)
                self.app.set_message(f"commit failed: {reason}" if reason
                                     else "commit failed")
            await self.app.refresh_git()
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
                    style = "class:shell.error"
                elif line.startswith("@@"):
                    style = "class:shell.command"
                else:
                    style = "class:shell.output"
                self.app.shell.append(line, style)
            self.app.switch_mode("shell")
        asyncio.ensure_future(do())

    def git_revert(self):
        entry = self.current()
        if entry is None or not self._require_repo():
            return
        self.app.confirm(
            f"Revert '{entry.name}'? Uncommitted changes will be lost.",
            lambda ok: self._do_revert(entry, ok),
        )

    def _do_revert(self, entry, ok):
        if not ok:
            self.app.set_message("revert cancelled")
            return

        async def do():
            rc, out = await git.revert(entry.path, self.app.cwd)
            if rc == 0:
                self.app.set_message(f"reverted: {entry.name}")
            else:
                self.app.set_message(f"revert failed: {out.strip()}")
            self.refresh_listing(select_name=entry.name)  # file changed on disk
            await self.app.refresh_git()
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
                items.append((f"{mark}{b}", lambda b=b: self._branch_menu(b)))
            self.app.open_menu("Branches", items)
        asyncio.ensure_future(do())

    def _branch_menu(self, name):
        """Per-branch actions: checkout, or delete (locally / on the remote)."""
        self.app.open_menu(f"Branch · {name}", [
            ("Checkout", lambda: self._do_checkout(name)),
            ("Delete locally", lambda: self._confirm_delete_branch(name, remote=False)),
            ("Delete remotely", lambda: self._confirm_delete_branch(name, remote=True)),
        ])

    def _do_checkout(self, name):
        async def do():
            rc, out = await git.checkout_branch(name, self.app.cwd)
            if rc == 0:
                self.refresh()  # the new branch may have a different working tree
                self.app.set_message(f"checked out: {name}")
            else:
                self.app.set_message(f"checkout failed: {out.strip()}")
        asyncio.ensure_future(do())

    def _confirm_delete_branch(self, name, remote):
        if remote:
            label = f"Delete remote branch 'origin/{name}'? This affects the remote."
        else:
            label = f"Delete local branch '{name}'? This cannot be undone."
        self.app.confirm(label, lambda ok: self._do_delete_branch(name, remote, ok))

    def _do_delete_branch(self, name, remote, ok):
        if not ok:
            self.app.set_message("delete cancelled")
            return

        async def do():
            if remote:
                # Deleting a remote branch contacts the server and may prompt for
                # a username/password. Run it on a real terminal (like the shell
                # does for push/pull) — a piped run_git would hang or fail with
                # "could not read Username" where credentials are required.
                self.app.set_message(f"deleting remote branch: {name}…")
                rc = await self.app.runner.run_in_term(
                    f'git push origin --delete "{name}"')
                if rc == 0:
                    self.app.set_message(f"deleted remote branch: origin/{name}")
                else:
                    self.app.set_message(f"delete remote failed (exit {rc})")
            else:
                rc, out = await git.delete_local_branch(name, self.app.cwd)
                if rc == 0:
                    self.app.set_message(f"deleted local branch: {name}")
                else:
                    self.app.set_message(f"delete failed: {out.strip()}")
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    # -- key bindings ---------------------------------------------------------
    def _build_key_bindings(self):
        kb = KeyBindings()

        # Inline rename: while editing, these eager bindings capture every key so
        # the normal navigation/action keys (j, D, …) become plain text input.
        renaming = Condition(lambda: self._renaming)

        @kb.add(Keys.Any, filter=renaming, eager=True)
        def _(event):
            data = event.data
            if data and data.isprintable():
                self._rename_insert(data)

        @kb.add("backspace", filter=renaming, eager=True)
        def _(event):
            self._rename_backspace()

        @kb.add("delete", filter=renaming, eager=True)
        def _(event):
            self._rename_delete()

        @kb.add("left", filter=renaming, eager=True)
        def _(event):
            self._rename_move(-1)

        @kb.add("right", filter=renaming, eager=True)
        def _(event):
            self._rename_move(1)

        @kb.add("home", filter=renaming, eager=True)
        @kb.add("c-a", filter=renaming, eager=True)
        def _(event):
            self._rename_set_pos(0)

        @kb.add("end", filter=renaming, eager=True)
        @kb.add("c-e", filter=renaming, eager=True)
        def _(event):
            self._rename_set_pos(len(self._rename_text))

        @kb.add("enter", filter=renaming, eager=True)
        def _(event):
            self._rename_commit()

        @kb.add("escape", filter=renaming, eager=True)
        @kb.add("c-c", filter=renaming, eager=True)
        def _(event):
            self._rename_cancel()

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

        # [general] right_expand: when true (the default) Right/l fold/unfold a
        # directory inline and the expand key (e) enters it; when false they swap,
        # so Right/l enter the directory and e expands it. Enter always opens /
        # enters regardless.
        right_expand = (self.app.settings.get("right_expand") or "").strip().lower() \
            not in ("false", "0", "no", "off")
        right_handler = self.expand_or_open if right_expand else self.open
        expand_key_handler = self.open if right_expand else self.toggle_expand

        @kb.add("enter")
        def _(event):
            self.open()

        @kb.add("l")
        @kb.add("right")
        def _(event):
            right_handler()

        @kb.add("h")
        @kb.add("left")
        @kb.add("backspace")
        def _(event):
            self.collapse_or_up()

        # Configurable action keys (remappable via the [keys] section of nshrc).
        actions = {
            "copy": self.copy_entry,
            "cut": self.cut_entry,
            "paste": self.paste,
            "delete": self.delete_entry,
            "rename": self.rename_entry,
            "expand": expand_key_handler,
            "new_dir": self.new_dir,
            "new_file": self.new_file,
            "select": self.toggle_select,
            "menu": self.open_command_menu,
            "bookmark": lambda: self.app.open_bookmark_menu(),
            "home": lambda: self.app.go_home(),
            "visited": lambda: self.app.open_visited_menu(),
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
