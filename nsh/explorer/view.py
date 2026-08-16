"""Interactive file-explorer pane.

A focusable ``FormattedTextControl`` renders the current directory; navigation
and the lazygit-style Git keys (Space/c/d) are bound on the control so they are
only active while the explorer has focus.
"""
import asyncio
import fnmatch
import os
import stat
from pathlib import Path

from prompt_toolkit.application.current import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import DynamicKeyBindings, KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import ScrollOffsets, Window
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseModifier

from .. import config
from ..util import hangul
from ..util.aio import run_in_thread
from ..util.menu import SEPARATOR
from ..util.paths import human_size, shorten_home
from ..util.widgets import WheelScrollControl, visible_slice
from ..util.width import char_width, cut_to_width, pad_to_width, text_width
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
    def __init__(self, app, cwd):
        self.app = app
        # the shell tab that owns this pane, set by ShellTabs._new_tab. Git
        # output (commit / diff) goes to *this* tab's scrollback rather than the
        # globally-active one, so a commit run from a background tab still logs
        # in its own tab. None for a stray explorer not attached to a tab.
        self.session = None
        self.cwd = Path(cwd)  # this pane's own directory (panes can differ)
        # this pane's own git status (each pane can be in a different repo); the
        # app's git_status property points at the active pane's
        self.git_status = git.GitStatus()
        self.entries = []
        self.cursor = 0
        self._top = 0  # first rendered row (windowing); see util.widgets
        self.show_hidden = False
        # sort order (name|size|date|type) + reverse, seeded from nshrc
        self.sort = app.settings.get("sort", "name")
        if self.sort not in model.SORT_KEYS:
            self.sort = "name"
        self.reverse = (app.settings.get("sort_reverse", "false").strip().lower()
                        in ("true", "1", "yes", "on"))
        self.selected = set()  # set[Path] of marked entries (multi-select)
        # pattern-select (the '*' dialog): the selection as it was before the
        # dialog opened (matches are added on top, live), and the debounce task
        self._select_base = set()
        self._select_task = None
        self.expanded = set()  # set[Path] of directories expanded inline (tree)
        # set[Path] of rows currently flashing, plus the blink task — a brief
        # highlight on copy/cut so the action is visible (see _flash_paths)
        self._flash = set()
        self._flash_task = None
        self._signature = ()   # snapshot used to auto-refresh on external change
        self._git_signature = ()  # .git state can change without worktree files
        self._watch_scan_running = False
        # inline rename state: edits the cursor row's name in place (no dialog)
        self._renaming = False
        self._rename_entry = None
        self._rename_text = ""
        self._rename_pos = 0   # cursor index within _rename_text

        # wrapped in DynamicKeyBindings so a live config reload (rebuild_keys)
        # can swap the remapped action keys without restarting nsh
        self._kb = self._build_key_bindings()
        self.control = WheelScrollControl(
            lambda d: self.move(d * 3),  # mouse wheel moves the cursor
            on_click=self._on_mouse,     # click selects, double-click opens
            text=self._formatted_text,
            focusable=True,
            show_cursor=False,
            key_bindings=DynamicKeyBindings(lambda: self._kb),
            get_cursor_position=lambda: Point(0, self.cursor - self._top),
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

    # the copy / cut buffer lives on the app, not the view, so a copy in one
    # tab can be pasted in another (see NshApp.clipboard).
    @property
    def clipboard(self):
        return self.app.clipboard

    @clipboard.setter
    def clipboard(self, value):
        self.app.clipboard = value

    @property
    def _shell(self):
        """The shell scrollback git output from this pane belongs in: this pane's
        own tab, so a commit/diff run from a background tab logs in that tab
        rather than whichever tab happens to be active when it finishes."""
        return self.session or self.app.shell

    # -- data -----------------------------------------------------------------
    @staticmethod
    def _sig(entries):
        return tuple((e.name, e.is_dir, e.size, e.mtime, e.link_target)
                     for e in entries)

    @staticmethod
    def _display_name(entry):
        if entry.is_parent:
            return ".."
        suffix = os.sep if entry.is_dir else ""
        if entry.is_link:
            target = entry.link_target or "?"
            if entry.is_dir:
                target = target.rstrip("/\\") + suffix
            return f"{entry.name}{suffix} -> {target}"
        return entry.name + suffix

    def _list(self):
        """The cwd listing flattened into a tree: each expanded directory's
        contents follow it, indented one level deeper. A ``..`` row is prepended
        when the cwd has a parent, so you can step up without the keyboard."""
        return self._list_snapshot(
            self.cwd, self.show_hidden, self.sort, self.reverse,
            frozenset(self.expanded))

    @classmethod
    def _list_snapshot(cls, cwd, show_hidden, sort, reverse, expanded):
        """Build a listing from immutable pane state, safe for a worker."""
        cwd = Path(cwd)

        def flatten(directory, depth):
            out = []
            for entry in model.list_dir(
                    directory, show_hidden, sort, reverse):
                entry.depth = depth
                out.append(entry)
                # Recurse only through directories expanded in this immutable
                # snapshot, including directory symlinks as before.
                if entry.is_dir and entry.path in expanded:
                    out.extend(flatten(entry.path, depth + 1))
            return out

        entries = flatten(cwd, 0)
        parent = cwd.parent
        if parent != cwd:  # not already at a filesystem / drive root
            entries.insert(0, model.Entry(
                path=parent, name="..", is_dir=True, is_link=False,
                is_exec=False, is_image=False, size=0, is_parent=True))
        return entries

    def first_index(self):
        """The first selectable row — skips a leading ``..`` so entering a
        directory lands on real content rather than the parent shortcut."""
        return 1 if (self.entries and self.entries[0].is_parent
                     and len(self.entries) > 1) else 0

    def load(self):
        self.entries = self._list()
        self._signature = self._sig(self.entries)
        self._git_signature = self._git_watch_signature(self.entries)
        if self.cursor >= len(self.entries):
            self.cursor = max(0, len(self.entries) - 1)
        if self.cursor == 0:  # don't rest on the leading ".." by default
            self.cursor = self.first_index()

    def _apply_listing(self, entries):
        """Swap in a fresh listing, keeping the cursor on the same entry."""
        cur = self.current()
        cur_path = cur.path if cur else None
        self.entries = entries
        self._signature = self._sig(entries)
        if self.selected:  # drop selections that no longer exist
            self.selected &= {e.path for e in entries}
        # default to the first real row; restore the previous entry if it's still
        # here (including ".." itself, so a refresh keeps the cursor put)
        self.cursor = self.first_index()
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

    def _git_watch_signature(self, entries):
        children = tuple(entry.path for entry in entries
                         if entry.is_dir and not entry.is_parent)
        return git.metadata_signature(self.cwd, children)

    @classmethod
    def _watch_snapshot(cls, cwd, show_hidden, sort, reverse, expanded):
        entries = cls._list_snapshot(cwd, show_hidden, sort, reverse, expanded)
        children = tuple(entry.path for entry in entries
                         if entry.is_dir and not entry.is_parent)
        return entries, git.metadata_signature(cwd, children)

    async def check_external_change(self):
        """Re-list in a worker, discarding stale or overlapping scans."""
        if self._watch_scan_running:
            return False
        snapshot = (
            self.cwd, self.show_hidden, self.sort, self.reverse,
            frozenset(self.expanded),
        )
        self._watch_scan_running = True
        try:
            entries, git_signature = await run_in_thread(
                self._watch_snapshot, *snapshot)
        finally:
            self._watch_scan_running = False
        current = (
            self.cwd, self.show_hidden, self.sort, self.reverse,
            frozenset(self.expanded),
        )
        if current != snapshot:
            return False
        listing_changed = self._sig(entries) != self._signature
        git_changed = git_signature != self._git_signature
        if not listing_changed and not git_changed:
            return False
        if listing_changed:
            self._apply_listing(entries)
            self.app.invalidate()
        self._git_signature = git_signature
        asyncio.ensure_future(self.app.refresh_git())
        return True

    def current(self):
        if 0 <= self.cursor < len(self.entries):
            return self.entries[self.cursor]
        return None

    def cursor_name_end_col(self):
        """The column (0-based) just past the cursor row's name, so a popup can be
        offset right of it and leave the filename visible. Mirrors the row layout
        in _formatted_text: sel(2) + git marker(1) + space(1) + tree indent
        (2/level) + icon(1) + space(1), then the name's display width."""
        e = self.current()
        if e is None:
            return 0
        name = self._display_name(e)
        return 6 + 2 * e.depth + text_width(name)

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

    @classmethod
    def _git_marker_style(cls, code, marker_style, row_style, on_cursor):
        # RC/RD are blank cells whose background *is* the marker. Reversing that
        # style on the cursor row turns the colour into an invisible foreground.
        if code in ("RC", "RD"):
            return marker_style
        return cls._cursor_style(marker_style or row_style, on_cursor)

    def _cursor_visible(self):
        if self.app.mode in ("shell", "remote-shell"):
            return False
        if self.app.mode == "network":
            network = getattr(self.app, "networkview", None)
            return (network is not None and self is network.local_view and
                    self.app.network_local_focused())
        return self is self.app.explorer

    def _formatted_text(self):
        if not self.entries:
            return [("class:explorer.file", "  (empty directory)")]
        # Derive this pane's width from the layout directly. Reading the Window's
        # render_info instead would lag one frame behind any width change (app
        # start, toggling the preview, zooming), briefly pushing the size column
        # off-pane. list_cols mirrors the split weights, so a zoomed (9:1) pane
        # pads to its real width instead of an assumed even split.
        try:
            total = get_app().output.get_size().columns
        except Exception:
            total = 80
        cols = self.app.list_cols(self, total)
        # sel(2) + marker(2) + icon(2) + gap(1) + size
        name_w = max(4, cols - 7 - SIZE_COL)
        gs = self.git_status  # this pane's own status (works for the inactive pane too)
        # hide the cursor-row highlight while the shell has focus: the listing
        # is still shown on top of the shell, but the active "cursor" is the
        # command line, so highlighting an explorer row would be misleading.
        # In two-pane mode only the active pane shows its cursor row.
        cursor_shown = self._cursor_visible()
        self._top, end = visible_slice(
            self.window, len(self.entries), self.cursor, self._top)
        result = []
        for i in range(self._top, end):
            e = self.entries[i]
            on = cursor_shown and (i == self.cursor)
            sel = e.path in self.selected
            code = self._entry_git_code(e)
            marker = config.GIT_SYMBOL.get(code, " ")
            mstyle = config.GIT_STYLE.get(code, "")
            estyle = "class:explorer.selected" if sel else config.entry_style(e)
            # a copy/cut briefly flashes its rows: the whole row takes the flash
            # style and the cursor "reverse" is dropped so the highlight is a
            # single solid bar (the marker falls back to the row style too)
            if e.path in self._flash:
                estyle = "class:explorer.flash"
                mstyle = ""
                on = False
            name = self._display_name(e)
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
            # expanded directories get a down-pointing caret instead of ▸; the
            # ".." row has no caret (it isn't expandable)
            if e.is_parent:
                icon = " "
            else:
                icon = "▾" if (e.is_dir and e.path in self.expanded) else config.entry_icon(e)
            # the row being renamed shows an editable name cell instead of the name
            if self._renaming and on:
                name_frags = self._rename_name_fragments(nw)
            else:
                name_frags = [(self._cursor_style(estyle, on), pad_to_width(name, nw))]
            result += [
                # the leading marker cell uses the row style (not "") when
                # unselected, so the cursor-row "reverse" tints it the same as the
                # rest of the row instead of the inherited near-white default
                (self._cursor_style("class:explorer.selected" if sel else estyle, on),
                 "● " if sel else "  "),
                # the marker keeps its own colour when there is a git mark; with
                # no mark it falls back to the row style so the cursor-row
                # "reverse" doesn't leave a near-white blank cell. The trailing
                # gap uses the row style too, so the mark colour doesn't bleed.
                (self._git_marker_style(code, mstyle, estyle, on), marker),
                (self._cursor_style(estyle, on), " " + indent),
                (self._cursor_style(estyle, on), f"{icon} "),
                *name_frags,
                (self._cursor_style(estyle, on), " "),
                (self._cursor_style(size_style, on),
                 pad_to_width(size, SIZE_COL, align="right")),
            ]
            if i != end - 1:
                result.append(("", "\n"))
        return result

    def _entry_git_code(self, entry):
        """Display status for repo contents or child repos outside a repo."""
        gs = self.git_status
        if gs is None:
            return None
        return gs.display_code(
            entry.path, is_dir=entry.is_dir,
            expanded=entry.path in self.expanded, is_parent=entry.is_parent)

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
        if entry.is_parent:
            # step up, landing the cursor on the directory we're leaving
            self.app.set_cwd(self.cwd.parent, select_name=self.cwd.name)
        elif entry.is_dir:
            self.app.set_cwd(entry.path)
        else:
            self.app.open_file(entry.path)

    def _on_mouse(self, mouse_event):
        """Left-click moves the cursor to the clicked row (and activates this
        pane in two-pane view). Ctrl+click toggles the row's selection; clicking
        a directory's ▸/▾ caret expands or collapses it inline; a double-click
        anywhere else on the row opens it."""
        if self.app.consume_menu_click():
            return
        # any click in this pane activates it (and closes the shell if focused),
        # even on the empty area below the listing
        self.app.focus_pane(self)
        idx = self._top + mouse_event.position.y
        if not (0 <= idx < len(self.entries)):
            self.app.invalidate()
            return
        self.cursor = idx
        entry = self.entries[idx]
        # Ctrl+click toggles this row's multi-selection (like Space, but it stays
        # put and never opens/expands)
        if MouseModifier.CONTROL in getattr(mouse_event, "modifiers", frozenset()):
            self._toggle_selection(entry)
            self.app.invalidate()
            return
        # the caret sits at: sel marker (2) + git marker (1) + leading space (1)
        # + indent (2 per depth) — see _formatted_text's row layout
        caret_col = 4 + 2 * entry.depth
        if entry.is_dir and not entry.is_parent and mouse_event.position.x == caret_col:
            self.toggle_expand()
        elif self.app.double_click(("explorer", id(self)), idx):
            self.open()
        self.app.invalidate()

    def toggle_expand(self):
        """Expand/collapse the directory under the cursor inline (tree view).

        Symlinks to directories (is_dir=True, is_link=True) are expandable too;
        symlinks to files (is_dir=False) are not.
        """
        entry = self.current()
        if entry is None or not entry.is_dir or entry.is_parent:
            return
        if entry.path in self.expanded:
            self.expanded.discard(entry.path)
        else:
            self.expanded.add(entry.path)
        # rebuild the flattened listing, keeping the cursor on this directory
        self._apply_listing(self._list())
        self.app.preview.clear()
        self.app.schedule_git()  # newly visible nested directories may be repos
        self.app.invalidate()

    def collapse_or_up(self):
        """Left/h/backspace: fold an expanded directory, else move toward the root.

        - on an expanded directory   -> collapse it (keep the cursor on it)
        - inside an expanded subtree  -> move the cursor up to the parent
          directory (without folding it)
        - otherwise (top level)       -> change to the parent directory
        """
        entry = self.current()
        if entry is not None and entry.is_dir and entry.path in self.expanded:
            self.expanded.discard(entry.path)
            self._apply_listing(self._list())  # cursor stays on this directory
            self.app.preview.clear()
            self.app.schedule_git()
            self.app.invalidate()
            return
        if entry is not None and entry.depth > 0:
            # move the cursor to the parent directory, leaving it expanded
            parent = entry.path.parent
            for i, e in enumerate(self.entries):
                if e.path == parent:
                    self.cursor = i
                    break
            self.app.preview.clear()
            self.app.invalidate()
            return
        # going up: land the cursor on the directory we're leaving
        self.app.set_cwd(self.cwd.parent, select_name=self.cwd.name)

    # -- selection ------------------------------------------------------------
    def toggle_select(self):
        entry = self.current()
        if entry is None:
            return
        self._toggle_selection(entry)
        self.move(1)  # toggle-and-advance
        self.app.invalidate()

    def _toggle_selection(self, entry):
        if entry.is_parent:  # the ".." row is navigation only, never selectable
            return
        if entry.path in self.selected:
            self.selected.discard(entry.path)
        else:
            self.selected.add(entry.path)

    def clear_selection(self):
        if self.selected:
            self.selected.clear()
            self.app.set_message("selection cleared")
            self.app.invalidate()

    # -- pattern select ('*') -------------------------------------------------
    def select_pattern(self):
        """Open a dialog; entries whose name matches the typed pattern are
        selected live as you type. A pattern with no wildcard is a substring
        match (``txt`` grabs every name containing ``txt``); one with a glob
        metacharacter is matched as a shell glob (``*.py``, ``test?``). Matches
        are added on top of any existing selection; Esc restores it."""
        if not self.entries:
            return
        self._select_base = set(self.selected)
        self.app.open_input_dialog(
            "Select pattern", "", 0,
            self._select_pattern_commit,
            on_change=self._select_pattern_changed,
            on_cancel=self._select_pattern_cancel,
        )

    def _pattern_matches(self, pattern):
        """The set of entry paths matching ``pattern`` (case-insensitive)."""
        pat = pattern.strip().lower()
        if not pat:
            return set()
        if any(c in pat for c in "*?["):
            return {e.path for e in self.entries
                    if not e.is_parent and fnmatch.fnmatchcase(e.name.lower(), pat)}
        return {e.path for e in self.entries
                if not e.is_parent and pat in e.name.lower()}

    def _set_pattern_selection(self, pattern):
        self.selected.clear()
        self.selected.update(self._select_base)
        self.selected.update(self._pattern_matches(pattern))

    def _select_pattern_changed(self, pattern):
        # Debounce: coalesce rapid keystrokes so a very long listing isn't
        # rescanned on every keypress (which would stall typing). The scan and
        # the redraw only happen once typing pauses briefly.
        if self._select_task and not self._select_task.done():
            self._select_task.cancel()
        self._select_task = asyncio.ensure_future(self._apply_pattern(pattern))

    async def _apply_pattern(self, pattern):
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            return
        self._set_pattern_selection(pattern)
        self.app.invalidate()

    def _cancel_pattern_task(self):
        if self._select_task and not self._select_task.done():
            self._select_task.cancel()
        self._select_task = None

    def _select_pattern_commit(self, pattern):
        self._cancel_pattern_task()
        self._set_pattern_selection(pattern)  # apply the final text immediately
        n = len(self.selected)
        # land the cursor on the first selected entry (in listing order)
        for i, e in enumerate(self.entries):
            if e.path in self.selected:
                self.cursor = i
                break
        self.app.set_message(f"{n} selected" if n else "selection cleared")
        self.app.invalidate()

    def _select_pattern_cancel(self):
        self._cancel_pattern_task()
        self.selected.clear()
        self.selected.update(self._select_base)  # restore the pre-dialog selection
        self.app.invalidate()

    def toggle_hidden(self):
        self.show_hidden = not self.show_hidden
        self.app.preview.clear()
        self.load()
        self.app.invalidate()

    # -- sort order -----------------------------------------------------------
    def open_sort_menu(self):
        labels = [("Name", "name"), ("Size", "size"), ("Date", "date"), ("Type", "type")]
        # each key gets an ascending (↑) and a descending (↓) entry; the active
        # one is bulleted. ↑ is the natural order (A→Z, small→large, old→new).
        items = []
        for label, mode in labels:
            for arrow, rev in (("↑", False), ("↓", True)):
                active = mode == self.sort and rev == self.reverse
                items.append((("● " if active else "  ") + label + arrow,
                              lambda mode=mode, rev=rev: self.set_sort(mode, rev)))
        self.app.open_menu("Sort by", items)

    def set_sort(self, mode, reverse=False):
        self.sort = mode
        self.reverse = reverse
        self._resort()

    def _resort(self):
        self._apply_listing(self._list())  # re-list, keeping the cursor entry
        self.app.preview.clear()
        self.app.invalidate()

    def _targets(self):
        """Paths a file op should act on: the selection, else the cursor entry."""
        if self.selected:
            # preserve listing order, drop anything that has since vanished
            return [e.path for e in self.entries if e.path in self.selected]
        entry = self.current()
        return [entry.path] if entry and not entry.is_parent else []

    # -- file operations ------------------------------------------------------
    def _show_operation_errors(self, title, errors):
        if not errors:
            return
        lines = [str(error) for error in errors]
        show_error = getattr(self.app, "show_error", None)
        if show_error:
            show_error(title, lines)
        else:
            self.app.set_message(lines[-1])

    def _flash_paths(self, paths):
        """Briefly blink ``paths`` in the listing so a copy / cut is visible.

        Selection is cleared by copy/cut, so the rows would otherwise change with
        no on-screen feedback; this highlights them once for a fraction of a
        second. A new flash cancels any in-flight one."""
        paths = set(paths)
        if self._flash_task and not self._flash_task.done():
            self._flash_task.cancel()

        async def blink():
            try:
                self._flash = paths  # on
                self.app.invalidate()
                await asyncio.sleep(0.18)
            except asyncio.CancelledError:
                pass
            finally:
                self._flash = set()  # off
                self.app.invalidate()
        self._flash_task = asyncio.ensure_future(blink())

    def copy_entry(self):
        targets = self._targets()
        if not targets:
            return
        self.clipboard = (targets, "copy")
        self.selected.clear()
        self._flash_paths(targets)
        self.app.set_message(f"copied {len(targets)} item(s)  (p to paste)")
        self.app.invalidate()

    def cut_entry(self):
        targets = self._targets()
        if not targets:
            return
        self.clipboard = (targets, "cut")
        self.selected.clear()
        self._flash_paths(targets)
        self.app.set_message(f"cut {len(targets)} item(s)  (p to paste)")
        self.app.invalidate()

    def _target_dir(self, require_expanded=False):
        """The directory to act on, based on the cursor: a directory under the
        cursor is targeted *inside* (paste/create lands within it); a file
        targets its containing directory. In the tree view that may be an
        expanded subdirectory rather than the top-level cwd, so paste / new file
        / new folder follow the cursor. Falls back to this pane's directory when
        the cursor is on ``..`` or there's no entry.

        With ``require_expanded`` (paste), a *collapsed* directory under the
        cursor is not descended into — the item lands in its container instead,
        so you only paste inside a directory you've actually opened."""
        entry = self.current()
        if entry is None or entry.is_parent:
            return self.cwd
        if entry.is_dir:
            if require_expanded and entry.path not in self.expanded:
                return entry.path.parent  # collapsed: target its container
            return entry.path
        return entry.path.parent

    def _reveal_target(self, require_expanded=False):
        """Make a target directory under the cursor visible after the listing
        refreshes — expand it inline so a paste / new item created inside it
        shows up (and can be selected) rather than hiding in a collapsed row.

        Mirrors :meth:`_target_dir`: with ``require_expanded`` a collapsed
        cursor directory is left alone (the item went to its container, which is
        already visible), so it isn't force-expanded on paste."""
        entry = self.current()
        if entry is not None and entry.is_dir and not entry.is_parent:
            if require_expanded and entry.path not in self.expanded:
                return
            self.expanded.add(entry.path)

    def paste(self):
        if not self.clipboard:
            self.app.set_message("clipboard empty")
            return
        paths, op = self.clipboard
        dest = self._target_dir(require_expanded=True)

        async def do():
            done = 0
            last = None
            errors = []
            for i, src in enumerate(paths, 1):
                if not src.exists():
                    continue
                try:
                    if op == "copy":
                        self.app.set_message(f"copying {i}/{len(paths)}: {src.name}…")
                        last = await fileops.copy(src, dest)
                    else:
                        self.app.set_message(f"moving {i}/{len(paths)}: {src.name}…")
                        last = await fileops.move(src, dest)
                    done += 1
                except Exception as exc:  # noqa: BLE001 - surfaced to the user
                    errors.append(f"{src.name}: {exc}")
            if op == "cut":
                self.clipboard = None
            self._reveal_target(require_expanded=True)  # expand only if pasted inside
            self.refresh_listing(select_name=last.name if last else None)
            verb = "copied" if op == "copy" else "moved"
            self.app.set_message(f"{verb} {done}/{len(paths)} item(s)")
            self._show_operation_errors(f"{verb.title()} failed", errors)
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    # -- two-pane copy / move to the other pane -------------------------------
    # In two-pane view the copy (y) / cut (x) keys act straight across to the
    # other pane's directory (no clipboard); a confirm dialog guards the
    # operation since it's easy to hit the wrong key.
    def copy_action(self):
        if self.app.two_pane:
            self._transfer_to_other_pane("copy")
        else:
            self.copy_entry()

    def cut_action(self):
        if self.app.two_pane:
            self._transfer_to_other_pane("move")
        else:
            self.cut_entry()

    def _other_pane(self):
        return self.app.explorers[1 - self.app.active_pane]

    def _transfer_to_other_pane(self, op):
        targets = self._targets()
        if not targets:
            return
        dest = self._other_pane().cwd
        # moving onto the same directory is a no-op (it would just rename in
        # place), so block it; copying there is fine — it makes a "… copy"
        # duplicate via fileops.unique_target.
        if op == "move" and dest == self.cwd:
            self.app.set_message("both panes are the same directory")
            return
        verb = "Copy" if op == "copy" else "Move"
        if len(targets) == 1:
            label = f"{verb} '{targets[0].name}' to {shorten_home(dest)}?"
        else:
            label = f"{verb} {len(targets)} items to {shorten_home(dest)}?"
        self.app.confirm(
            label, lambda ok: self._do_transfer(targets, dest, op) if ok else
            self.app.set_message(f"{verb.lower()} cancelled"))

    def _do_transfer(self, targets, dest, op):
        async def do():
            done = 0
            last = None
            errors = []
            for i, src in enumerate(targets, 1):
                if not src.exists():
                    continue
                try:
                    if op == "copy":
                        self.app.set_message(f"copying {i}/{len(targets)}: {src.name}…")
                        last = await fileops.copy(src, dest)
                    else:
                        self.app.set_message(f"moving {i}/{len(targets)}: {src.name}…")
                        last = await fileops.move(src, dest)
                    done += 1
                except Exception as exc:  # noqa: BLE001 - surfaced to the user
                    errors.append(f"{src.name}: {exc}")
            self.selected.clear()
            # both panes changed: the destination gained files, and on a move the
            # source lost them
            self._other_pane().load()
            self.refresh_listing()
            verb = "copied" if op == "copy" else "moved"
            self.app.set_message(f"{verb} {done}/{len(targets)} item(s) to other pane")
            self._show_operation_errors(f"{verb.title()} failed", errors)
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

    def trash_entry(self):
        targets = self._targets()
        if not targets:
            return
        if len(targets) == 1:
            label = f"Move '{targets[0].name}' to the Trash?"
        else:
            label = f"Move {len(targets)} items to the Trash?"
        self.app.confirm(label, lambda ok: self._do_trash(targets, ok))

    def _do_trash(self, targets, ok):
        if not ok:
            self.app.set_message("trash cancelled")
            return

        async def do():
            done = 0
            errors = []
            for path in targets:
                try:
                    await fileops.trash(path)
                    done += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{path.name}: {exc}")
            self.selected.clear()
            self.refresh_listing()
            self.app.set_message(f"moved {done} item(s) to Trash")
            self._show_operation_errors("Move to Trash failed", errors)
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    def _do_delete(self, targets, ok):
        if not ok:
            self.app.set_message("delete cancelled")
            return

        async def do():
            done = 0
            errors = []
            for path in targets:
                try:
                    await fileops.delete(path)
                    done += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{path.name}: {exc}")
            self.selected.clear()
            self.refresh_listing()
            self.app.set_message(f"deleted {done} item(s)")
            self._show_operation_errors("Delete failed", errors)
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    def send_to_shell(self):
        """Open the shell with the target file name(s) — the selection, else the
        cursor file — spliced into the command line (Ctrl+J / Ctrl+Down)."""
        self.app.shell_insert_paths(self._targets())

    def chmod_entry(self):
        """Open the permission grid for the target(s), seeded from the first
        one's current mode, and apply the chosen mode to all of them."""
        targets = self._targets()
        if not targets:
            return
        try:
            mode = stat.S_IMODE(os.stat(targets[0]).st_mode)
        except OSError:
            mode = 0o644
        name = (targets[0].name if len(targets) == 1
                else f"{len(targets)} items")
        self.app.open_chmod_dialog(
            f"Permissions · {name}", mode,
            lambda m: self._do_chmod(targets, m))

    def _do_chmod(self, targets, mode):
        done, err = 0, None
        for path in targets:
            try:
                os.chmod(path, mode)
                done += 1
            except OSError as exc:
                err = f"{path.name}: {exc}"
        self.selected.clear()
        self.refresh_listing()
        if err:
            self._show_operation_errors("Chmod failed", [err])
        else:
            self.app.set_message(f"chmod {mode:03o} · {done} item(s)")
        self.app.invalidate()

    def rename_entry(self):
        """Begin editing the cursor row's name in place (no dialog)."""
        entry = self.current()
        if entry is None or entry.is_parent:
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
            self._show_operation_errors("Rename failed", [str(exc)])
        self.app.invalidate()

    def _rename_name_fragments(self, name_w):
        """Render the edited name as fragments exactly ``name_w`` cells wide,
        horizontally scrolled so the block cursor stays visible.

        Widths are measured in terminal cells, not characters, so wide (CJK)
        names scroll and pad correctly — a 한글 glyph occupies two cells."""
        text, pos = self._rename_text, self._rename_pos
        at = text[pos] if pos < len(text) else " "
        cw = char_width(at) or 1  # the block cursor always occupies >= 1 cell
        # scroll right by whole characters until the cursor cell fits in name_w
        start = 0
        while text_width(text[start:pos]) + cw > name_w:
            start += 1
        before = text[start:pos]
        before_w = text_width(before)
        after = cut_to_width(text[pos + 1:], max(0, name_w - before_w - cw))
        used = before_w + cw + text_width(after)
        pad = " " * max(0, name_w - used)
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
            # follow the cursor like paste: land inside an expanded directory,
            # else in its container (a collapsed dir is not descended into)
            target = fileops.make_dir(self._target_dir(require_expanded=True), name)
            self._reveal_target(require_expanded=True)
            self.refresh_listing(select_name=target.name)
            self.app.set_message(f"created folder: {target.name}")
        except Exception as exc:  # noqa: BLE001
            self._show_operation_errors("Create folder failed", [str(exc)])

    def new_file(self):
        self.app.open_input_dialog("New file", "", 0, self._do_new_file)

    def _do_new_file(self, name):
        name = name.strip()
        if not name:
            self.app.set_message("cancelled")
            return
        try:
            # follow the cursor like paste: land inside an expanded directory,
            # else in its container (a collapsed dir is not descended into)
            target = fileops.make_file(self._target_dir(require_expanded=True), name)
            self._reveal_target(require_expanded=True)
            self.refresh_listing(select_name=target.name)
            self.app.set_message(f"created file: {target.name}")
            asyncio.ensure_future(self.app.refresh_git())
        except Exception as exc:  # noqa: BLE001
            self._show_operation_errors("Create file failed", [str(exc)])

    def edit_entry(self):
        entry = self.current()
        if entry is None or entry.is_dir:
            return
        self.app.edit_file(entry.path)

    # -- action menu (Tab) ----------------------------------------------------
    def open_command_menu(self):
        cur = self.current()
        # With nothing selected yet, force-select the cursor entry so the menu's
        # actions have an explicit target. In particular Git: Commit then commits
        # that file; the whole directory is committed via Git: Commit all. This
        # forced mark is temporary — it's cleared once the menu closes.
        forced = None
        if not self.selected and cur is not None:
            self.selected.add(cur.path)
            forced = cur.path
            self.app.invalidate()
        has_target = bool(self.selected) or cur is not None
        if len(self.selected) == 1:
            target = next(iter(self.selected)).name
        elif self.selected:
            target = f"{len(self.selected)} selected"
        elif cur is not None:
            target = cur.name
        else:
            target = "(empty)"  # empty directory: only paste / git may apply
        items = []
        # entry actions need something under the cursor / a selection
        if has_target:
            # "Edit" only for a single text file under the cursor (not a
            # directory, image, or binary), and not while multi-selecting.
            if (len(self.selected) == 1 and cur is not None and cur.path in self.selected
                    and not cur.is_dir and not cur.is_image
                    and model.is_text_file(cur.path)):
                items.append(("Edit", self.edit_entry))
            items += [("Copy", self.copy_entry), ("Cut", self.cut_entry)]
        if self.clipboard:
            items.append(("Paste", self.paste))
        if has_target:
            items += [("Rename", self.rename_entry),
                      ("Move to Trash", self.trash_entry),
                      ("Delete permanently", self.delete_entry)]
            # Python exposes chmod on every supported platform. Windows only
            # honours its writable/read-only subset, but keeping the action in
            # the same menu makes the interface predictable across machines.
            items.append(("chmod…", self.chmod_entry))
        # Creation belongs to the contextual file menu. Keep it as a distinct
        # group between ordinary file actions (ending in chmod) and Git.
        items += [
            (SEPARATOR, None),
            ("New folder", self.new_dir),
            ("New file", self.new_file),
            (SEPARATOR, None),
        ]
        if self.app.git_status and self.app.git_status.is_repo:
            gs = self.app.git_status
            code = gs.code_for(cur.path) if cur else None
            if code == "?":
                # untracked file: stage/unstage, commit and diff don't apply
                # yet, so only offer to start tracking it.
                items.append(("Git: Add", self.git_stage))
            elif code in ("M", "S", "C"):
                # has changes (modified / staged / conflicted): full set.
                # Git: Commit here commits the selection (the forced cursor file)
                items += [
                    ("Git: Stage / Unstage", self.git_stage),
                    ("Git: Diff", self.git_diff),
                ]
            commit_paths = tuple(path for path in self.selected if path.is_file())
            commit_item = ((_git_action_label("Git: Commit", commit_paths),
                            self.git_commit) if commit_paths else None)
            revert_paths = tuple(path for path in self.selected
                                 if gs.code_for(path) != "?")
            revert_item = ((_git_action_label("Git: Revert", revert_paths),
                            lambda paths=revert_paths: self.git_revert(paths))
                           if revert_paths else None)
            # commit the whole directory ('.') whenever the repo has tracked
            # changes — the selection-based Git: Commit can no longer reach it
            # now that the cursor file is always force-selected
            if gs.dirty:
                items.append((_git_action_label("Git: Commit", ()),
                              self.git_commit_all))
                if commit_item is not None:
                    items.append(commit_item)
                items.append((_git_action_label("Git: Revert", ()),
                              lambda: self.git_revert(())))
                if revert_item is not None:
                    items.append(revert_item)
            # pull when there's an upstream; push when there are commits to push
            # (ahead of the upstream, or an unpushed branch on a repo with a remote)
            if gs.can_pull:
                items.append(("Git: Pull", self.git_pull))
            if gs.can_push:
                items.append(("Git: Push", self.git_push))
            if gs.in_progress:  # a merge/rebase is mid-flight: resolve it
                items.append((f"Git: Continue {gs.in_progress}", self.git_continue))
                items.append((f"Git: Abort {gs.in_progress}", self.git_abort))
            if gs.dirty:
                items.append(("Git: Stash", self.git_stash))
            if gs.has_stash:
                items.append(("Git: Stashes…", self.git_stash_menu))
            if gs.has_commits:
                log_paths = tuple(path for path in self.selected if path.is_file())
                items.append((_git_log_label(()),
                              lambda: self.app.open_log((self.app.cwd,))))
                if log_paths:
                    items.append((_git_log_label(log_paths),
                                  lambda paths=log_paths: self.app.open_log(paths)))
            items.append(("Git: Branches", self.git_branches))
        # clear the force-selected cursor file once the menu closes (whether an
        # action ran or it was cancelled); a real, user-made selection is kept
        on_close = (lambda: self._deselect_forced(forced)) if forced else None
        self.app.open_menu(f"Actions · {target}", items, on_close=on_close,
                           at_cursor=True)

    def _deselect_forced(self, path):
        """Drop a cursor file the menu force-selected. Deferred a tick so the
        chosen action (which runs after the menu closes) can still read it as
        part of the selection before it's removed."""
        def do():
            self.selected.discard(path)
            self.app.invalidate()
        try:
            asyncio.get_running_loop().call_soon(do)
        except RuntimeError:
            do()

    # -- help (?) -------------------------------------------------------------
    _NAV_HELP = [
        ("↑ ↓  k j", "move cursor"),
        ("↵", "open file / enter directory"),
        ("l  →", "expand / collapse directory"),
        ("⌫  h  ←", "parent directory"),
        ("g  G", "top / bottom"),
        ("PgUp PgDn", "page up / down"),
    ]
    _ACTION_HELP = [
        ("select", "select / deselect (multi-select)"),
        ("select_pattern", "select by pattern (glob / substring)"),
        ("two_pane", "toggle two-pane view"),
        ("pane_prev", "prev tab / switch pane / focus list"),
        ("pane_next", "next tab / switch pane / focus preview"),
        ("tab_move_prev", "move this tab left"),
        ("tab_move_next", "move this tab right"),
        ("zoom", "enlarge the focused pane (9:1)"),
        ("menu", "action menu (copy, rename, git…)"),
        ("copy", "copy"), ("cut", "cut"), ("paste", "paste"),
        ("rename", "rename (also i)"), ("new_dir", "new folder"), ("new_file", "new file"),
        ("trash", "move to trash"), ("delete", "delete permanently"),
        ("bookmark", "bookmarks"), ("sort", "sort order"),
        ("find", "fuzzy find"),
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

    def git_commit_all(self):
        """Commit the whole current directory (``git commit .``), regardless of
        what's selected — the menu's force-select makes plain Git: Commit target
        the cursor file, so this is how a directory-wide commit stays reachable."""
        if not self._require_repo():
            return
        self.app.open_input_dialog(
            "Commit message (whole directory)", "", 0,
            lambda msg: self._do_commit(msg, ["."]))

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
                self._shell.append(out.strip() or "committed", "class:shell.output")
                self.app.set_message("committed")
                sel = self.app.active_selection()  # consumed: clear the marks
                if sel:
                    sel.clear()
            else:
                # surface the real reason (identity unset, nothing to commit,
                # hook…) in the status bar; full output goes to the scrollback
                self._shell.append(out.strip() or "git commit failed",
                                   "class:shell.error")
                reason = _git_error_summary(out)
                self.app.set_message(f"commit failed: {reason}" if reason
                                     else "commit failed")
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    def git_pull(self):
        if not self._require_repo():
            return

        async def do():
            # pull may need credentials and can open a merge editor / hit
            # conflicts, so run it on a real terminal
            rc = await self.app.runner.run_in_term("git pull")
            self.app.set_message("pulled" if rc == 0 else "pull failed")
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    def git_push(self):
        if not self._require_repo():
            return
        gs = self.app.git_status

        async def do():
            # no upstream yet -> set it on this first push; otherwise plain push.
            if gs and not gs.has_upstream and gs.branch and gs.branch != "(detached)":
                cmd = f'git push -u origin "{gs.branch}"'
            else:
                cmd = "git push"
            # push can need credentials, so run it on a real terminal (like the
            # remote-branch delete) instead of through the piped runner
            rc = await self.app.runner.run_in_term(cmd)
            self.app.set_message("pushed" if rc == 0 else "push failed")
            await self.app.refresh_git()  # ahead count -> 0 after a clean push
        asyncio.ensure_future(do())

    # -- stash ----------------------------------------------------------------
    def _git_run(self, coro, ok_msg, fail_label):
        """Await a git op, report the outcome and refresh."""
        async def do():
            rc, out = await coro
            if rc == 0:
                self.app.set_message(ok_msg)
            else:
                reason = _git_error_summary(out)
                self.app.set_message(f"{fail_label} failed: {reason}" if reason
                                     else f"{fail_label} failed")
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    def git_stash(self):
        if not self._require_repo():
            return
        self._git_run(git.stash_push(self.app.cwd), "stashed", "stash")

    def git_stash_menu(self):
        if not self._require_repo():
            return

        async def build():
            entries = await git.stash_list(self.app.cwd)
            if not entries:
                self.app.set_message("no stashes")
                return
            items = [(desc, lambda ref=ref, desc=desc: self._stash_actions(ref, desc))
                     for ref, desc in entries]
            self.app.open_menu("Stashes", items)
        asyncio.ensure_future(build())

    def _stash_actions(self, ref, desc):
        cwd = self.app.cwd
        self.app.open_menu(cut_to_width(desc, 48), [
            ("Pop (apply + drop)",
             lambda: self._git_run(git.stash_pop(cwd, ref), "popped", "stash pop")),
            ("Apply (keep)",
             lambda: self._git_run(git.stash_apply(cwd, ref), "applied", "stash apply")),
            ("Drop",
             lambda: self.app.confirm(
                 f"Drop {ref}?",
                 lambda ok: self._git_run(git.stash_drop(cwd, ref), "dropped",
                                          "stash drop") if ok else None)),
        ])

    # -- merge/rebase in progress ---------------------------------------------
    def git_continue(self):
        op = self.app.git_status.in_progress if self.app.git_status else None
        if not op:
            return

        async def do():
            # continuing a merge is a commit; a rebase/cherry-pick/revert uses
            # --continue. Either may open an editor, so run on a real terminal.
            cmd = "git commit" if op == "merge" else f"git {op} --continue"
            rc = await self.app.runner.run_in_term(cmd)
            self.app.set_message(
                "continued" if rc == 0
                else f"{op} still in progress — resolve conflicts and stage them")
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    def git_abort(self):
        op = self.app.git_status.in_progress if self.app.git_status else None
        if not op:
            return
        self.app.confirm(
            f"Abort the {op}? Conflict resolution so far will be discarded.",
            lambda ok: self._git_run(git.abort_operation(self.app.cwd, op),
                                     f"{op} aborted", "abort") if ok else None)

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

    def git_revert(self, paths=None):
        if not self._require_repo():
            return
        if paths == ():
            selected = (Path("."),)
        elif paths is not None:
            selected = tuple(paths)
        else:
            selected = tuple(self.selected)
        if not selected:
            entry = self.current()
            selected = (entry.path,) if entry is not None else ()
        revert_paths = tuple(path for path in selected
                             if path == Path(".")
                             or self.app.git_status.code_for(path) != "?")
        if not revert_paths:
            self.app.set_message("nothing to revert")
            return
        scope = ("'.'" if revert_paths == (Path("."),)
                 else (f"'{revert_paths[0].name}'" if len(revert_paths) == 1
                       else f"{len(revert_paths)} files"))
        self.app.confirm(
            f"Revert {scope}? Uncommitted changes will be lost.",
            lambda ok: self._do_revert(revert_paths, ok),
        )

    def _do_revert(self, paths, ok):
        if not ok:
            self.app.set_message("revert cancelled")
            return

        async def do():
            done = 0
            error = ""
            for path in paths:
                rc, out = await git.revert(path, self.app.cwd)
                if rc == 0:
                    done += 1
                elif not error:
                    error = out.strip()
            if done == len(paths):
                self.app.set_message(f"reverted: {done} file(s)")
            else:
                self.app.set_message(f"reverted {done}/{len(paths)}: {error}")
            self.refresh_listing()
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
            branches, remotes, cur = await git.list_branches(self.app.cwd)
            items = [("+ New Branch", self.git_new_branch)]
            for b in branches:
                mark = "● " if b == cur else "  "
                items.append((f"{mark}{b}", lambda b=b: self._branch_menu(b)))
            for ref in remotes:
                items.append((f"⇣ {ref}",
                              lambda ref=ref: self._branch_menu(ref, remote=True)))
            self.app.open_menu("Branches", items)
        asyncio.ensure_future(do())

    def _branch_menu(self, name, remote=False):
        """Per-branch actions: checkout, browse its tree, or delete."""
        if remote:
            items = [
                ("Checkout (track)", lambda: self._do_checkout(name, remote=True)),
                ("Browse", lambda: self.app.open_branch_browser(name)),
                ("Delete remotely", lambda: self._confirm_delete_branch(
                    name, remote=True, remote_ref=name)),
            ]
            title = f"Remote branch · {name}"
        else:
            items = [
                ("Checkout", lambda: self._do_checkout(name)),
                ("Browse", lambda: self.app.open_branch_browser(name)),
                ("Delete locally", lambda: self._confirm_delete_branch(
                    name, remote=False)),
                ("Delete remotely", lambda: self._confirm_delete_branch(
                    name, remote=True, remote_ref="origin/" + name)),
            ]
            title = f"Branch · {name}"
        self.app.open_menu(title, items)

    def _do_checkout(self, name, remote=False):
        async def do():
            if remote:
                rc, out = await git.checkout_remote_branch(name, self.app.cwd)
            else:
                rc, out = await git.checkout_branch(name, self.app.cwd)
            if rc == 0:
                self.refresh()  # the new branch may have a different working tree
                self.app.set_message(f"checked out: {name}")
            else:
                self.app.set_message(f"checkout failed: {out.strip()}")
        asyncio.ensure_future(do())

    def _confirm_delete_branch(self, name, remote, remote_ref=None):
        if remote:
            remote_ref = remote_ref or "origin/" + name
            label = f"Delete remote branch '{remote_ref}'? This affects the remote."
        else:
            label = f"Delete local branch '{name}'? This cannot be undone."
        self.app.confirm(label, lambda ok: self._do_delete_branch(
            name, remote, ok, remote_ref=remote_ref))

    def _do_delete_branch(self, name, remote, ok, remote_ref=None):
        if not ok:
            self.app.set_message("delete cancelled")
            return

        async def do():
            if remote:
                # Deleting a remote branch contacts the server and may prompt for
                # a username/password. Run it on a real terminal (like the shell
                # does for push/pull) — a piped run_git would hang or fail with
                # "could not read Username" where credentials are required.
                ref = remote_ref or "origin/" + name
                remote_name, branch_name = ref.split("/", 1)
                self.app.set_message(f"deleting remote branch: {ref}…")
                rc = await self.app.runner.run_in_term(
                    f'git push "{remote_name}" --delete "{branch_name}"')
                if rc == 0:
                    self.app.set_message(f"deleted remote branch: {ref}")
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
    def rebuild_keys(self):
        """Rebuild the action-key bindings from the (reloaded) config; the
        DynamicKeyBindings wrapper on the control then uses them live."""
        self._kb = self._build_key_bindings()

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

        # send the cursor / selected file name(s) to the shell command line
        @kb.add("c-j")
        @kb.add("c-down")
        def _(event):
            self.send_to_shell()

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

        # Enter opens a file / enters a directory; Right (and l) folds or unfolds
        # the directory under the cursor inline (tree view). On a plain file —
        # which has nothing to expand — Right instead hands focus to the preview
        # pane (when it's on screen) so it reads as "step into the preview".
        @kb.add("enter")
        def _(event):
            self.open()

        @kb.add("l")
        @kb.add("right")
        def _(event):
            entry = self.current()
            if entry is not None and not entry.is_dir:
                self.app.focus_preview()
            else:
                self.toggle_expand()

        @kb.add("h")
        @kb.add("left")
        @kb.add("backspace")
        def _(event):
            self.collapse_or_up()

        # Shift+H / Shift+L move focus left / right across the on-screen
        # columns (vim-style): the two panes in two-pane view, or the list and
        # its preview in single-pane view — alongside click-to-focus.
        @kb.add("H")
        def _(event):
            self.app.move_pane_focus(-1)

        @kb.add("L")
        def _(event):
            self.app.move_pane_focus(1)

        # `i` always starts an inline rename (vim-ish "insert"), alongside the
        # remappable rename key. While editing, the eager Keys.Any binding above
        # swallows `i` as text, so this only fires in normal navigation.
        @kb.add("i")
        def _(event):
            self.rename_entry()

        # `'` opens the bookmarks menu too (the "jump" key from the original
        # nsh), alongside the remappable bookmark key.
        @kb.add("'")
        def _(event):
            self.app.open_bookmark_menu()

        # Configurable action keys (remappable via the [keys] section of nshrc).
        actions = {
            "copy": self.copy_action,
            "cut": self.cut_action,
            "paste": self.paste,
            "trash": self.trash_entry,
            "delete": self.delete_entry,
            "rename": self.rename_entry,
            "new_dir": self.new_dir,
            "new_file": self.new_file,
            "select": self.toggle_select,
            "select_pattern": self.select_pattern,
            "two_pane": lambda: self.app.toggle_two_pane(),
            "menu": self.open_command_menu,
            "bookmark": lambda: self.app.open_bookmark_menu(),
            "home": lambda: self.app.go_home(),
            "visited": lambda: self.app.open_visited_menu(),
            "sort": self.open_sort_menu,
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

        hangul.add_hangul_aliases(kb)  # let the keys work with the Korean IME on
        return kb

    @staticmethod
    def _action_handler(func):
        def handler(event):
            func()
        return handler


def _git_log_label(paths):
    """Action-menu label that distinguishes repository and file history."""
    return _git_action_label("Git: Log", paths)


def _git_action_label(action, paths):
    """Label an action with its file scope, or ``.`` for directory scope."""
    if len(paths) == 1:
        return f"{action} {paths[0].name}"
    if paths:
        return f"{action} {len(paths)} files"
    return f"{action} ."
