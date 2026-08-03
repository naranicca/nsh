"""Side preview pane.

Renders the entry under the explorer cursor: a directory's contents, a text
file's body (encoding auto-detected), an image's metadata (dimensions parsed
straight from the file header — no Pillow dependency), or a hexdump for binary
files. Reads happen in a worker thread and results are cached per path, so
scrolling the listing never blocks the UI.
"""
import asyncio
import os
import stat
from types import SimpleNamespace
import struct
from datetime import datetime

from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import Margin

from .. import config
from ..util.aio import run_in_thread
from ..util.paths import human_size, norm
from ..util.widgets import WheelScrollControl
from ..util.width import cut_to_width, text_width
from . import git, model

READ_BYTES = 64 * 1024   # cap how much of a file we pull in for preview
MAX_LINES = 400
MAX_DIR_ITEMS = 300
HEX_BYTES = 256


def image_dimensions(path):
    """(width, height) for common image formats, or ``None`` if unknown."""
    try:
        with open(path, "rb") as f:
            head = f.read(26)
            if len(head) >= 24 and head[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", head[16:24])
            if len(head) >= 10 and head[:6] in (b"GIF87a", b"GIF89a"):
                return struct.unpack("<HH", head[6:10])
            if len(head) >= 26 and head[:2] == b"BM":
                w, h = struct.unpack("<ii", head[18:26])
                return abs(w), abs(h)
            if head[:2] == b"\xff\xd8":
                return _jpeg_dimensions(path)
    except OSError:
        return None
    return None


def _jpeg_dimensions(path):
    with open(path, "rb") as f:
        f.read(2)
        while True:
            byte = f.read(1)
            while byte and byte != b"\xff":
                byte = f.read(1)
            marker = f.read(1)
            while marker == b"\xff":
                marker = f.read(1)
            if not marker:
                return None
            m = marker[0]
            if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                f.read(3)  # segment length (2) + sample precision (1)
                h, w = struct.unpack(">HH", f.read(4))
                return w, h
            seg = f.read(2)
            if len(seg) < 2:
                return None
            f.seek(struct.unpack(">H", seg)[0] - 2, 1)


def _sanitize(line: str) -> str:
    """Drop control characters that would corrupt the terminal."""
    return "".join(
        ch if (ch >= " " or ch == "\t") else "·" for ch in line
    ).expandtabs(4)


class _PreviewScrollbar(Margin):
    """Scrollbar for the preview pane. The pane windows its own content when
    focused, so the built-in margin can't see the full length — this reads the
    view's line counts and scroll position instead."""

    def __init__(self, view):
        self.view = view

    def get_width(self, get_ui_content):
        return 1 if self.view._sb_total > self.view._sb_view else 0

    def create_margin(self, window_render_info, width, height):
        total = self.view._sb_total
        if height <= 0 or total <= height:
            return []
        max_scroll = total - height
        scroll = max(0, min(self.view._scroll, max_scroll))
        thumb = max(1, min(height, height * height // total))
        top = (height - thumb) * scroll // max_scroll if max_scroll else 0
        top = max(0, min(top, height - thumb))
        # the thumb is coloured only while the pane is focused; otherwise it goes
        # grayscale so the focus state reads at a glance
        thumb_style = ("class:scrollbar.button" if self.view.app.preview_focused()
                       else "class:scrollbar.button.inactive")
        frags = []
        for row in range(height):
            inside = top <= row < top + thumb
            frags.append((thumb_style if inside
                          else "class:scrollbar.background", " "))
            if row < height - 1:
                frags.append(("", "\n"))
        return frags


class PreviewView:
    def __init__(self, app):
        self.app = app
        self._cache = {}
        self._inflight = set()
        self._diff_hunks = {}
        self._hunk_selection = {}
        self._conflict_undo = {}
        self._scroll = 0          # top visible line when the pane is focused
        self._scroll_id = None    # identity of what's scrolled (reset on change)
        self._sb_total = 0        # full document line count (for the scrollbar)
        self._sb_view = 0         # visible line count (for the scrollbar)
        self._btn_chars = 0       # char count of the header's [+]/[-] button row
        # focusable so F7/F8 (or a click) can move into the pane and scroll it
        # (the list keeps its own cursor); the pinned header is tinted while it's
        # focused.
        self.control = WheelScrollControl(
            lambda d: self.scroll(d * 3),  # wheel scrolls the preview
            on_click=self._on_mouse,       # a click focuses the pane
            text=self._visible_text, focusable=True, show_cursor=False,
            key_bindings=self._kb())
        self.window = Window(
            self.control,
            wrap_lines=True,
            style="class:preview",
            right_margins=[_PreviewScrollbar(self)],
            # match the explorer: preferred=0 keeps the split content-independent
            width=Dimension(min=0, preferred=0, weight=1),
        )

    # -- focus / scrolling ----------------------------------------------------
    def focus(self):
        self.app.application.layout.focus(self.control)
        self.app.invalidate()

    def _on_mouse(self, mouse_event):
        """A click focuses the preview pane (closing the shell if it was focused)
        — unless a menu is open, in which case the click just dismisses it (like
        Esc). The [+]/[-] zoom button in the header's top-right corner is handled
        here too (the control swallows per-fragment handlers)."""
        if self.app.consume_menu_click():
            return
        # the click column is a character index (not a display column), so test
        # against the button row's character count — the button is its last three
        # characters. This keeps it clickable when the header has wide (CJK) names.
        btn_chars = getattr(self, "_btn_chars", 0)
        if (mouse_event.position.y == 0 and btn_chars
                and mouse_event.position.x >= btn_chars - 3):
            self._toggle_zoom()
            return
        self.app.close_shell_if_open()
        # clicking the preview while the list is the zoomed pane cancels zoom
        # rather than handing the big share over (the [+]/[-] button is for that)
        if self.app.zoom and not self.app.preview_focused():
            self.app.zoom = False
        self.focus()

    def _visible_height(self):
        ri = getattr(self.window, "render_info", None)
        if ri is not None and ri.window_height:
            return ri.window_height
        return 20

    def _width(self):
        ri = getattr(self.window, "render_info", None)
        if ri is not None and getattr(ri, "window_width", 0):
            return ri.window_width
        return 40

    def _content_width(self):
        """The pane's true content width for the current frame.

        The control records the width it was last rendered at; that is lag-free,
        whereas ``render_info.window_width`` trails a frame behind the scrollbar
        appearing — which let the right-aligned header button spill onto a second
        row the moment a long file (or a wrapped name) first overflowed. Falls
        back to the render_info width before the first render."""
        w = getattr(self.control, "last_width", None)
        return w if w else self._width()

    @staticmethod
    def _focus_header(line, width):
        """Re-style a pinned header line with the focus background, padded to the
        full pane width so the highlight spans the whole row."""
        out = [((s + " class:preview.header.focus").strip(), t) for s, t in line]
        used = sum(text_width(t) for _, t in line)
        if width - used > 0:
            out.append(("class:preview.header.focus", " " * (width - used)))
        return out

    def _zoom_label(self):
        # [-] while the preview is the zoomed pane, [+] otherwise (click to zoom)
        return "[-]" if (self.app.zoom and self.app.preview_focused()) else "[+]"

    @staticmethod
    def _wrap_frags(line, first_width, rest_width):
        """Split a fragment line into multiple lines, wrapping by display width.
        The first wrapped line is limited to ``first_width`` (room for the button),
        the rest to ``rest_width``. Per-fragment styles are preserved."""
        lines, cur, used = [], [], 0
        limit = max(1, first_width)
        rest = max(1, rest_width)
        for style, text in line:
            text = text.replace("\n", "")
            while text:
                seg = cut_to_width(text, limit - used)
                if not seg:
                    if not cur and used == 0:
                        seg = text[:1]   # guarantee progress on a too-narrow line
                    else:
                        lines.append(cur)
                        cur, used, limit = [], 0, rest
                        continue
                cur.append((style, seg))
                used += text_width(seg)
                text = text[len(seg):]
                if used >= limit and text:
                    lines.append(cur)
                    cur, used, limit = [], 0, rest
        lines.append(cur)
        return lines

    def _line_with_button(self, line, width, focused):
        """Wrap a header line to the pane width and append a right-aligned [+]/[-]
        zoom button to the first row (pinned to the top-right corner). A long
        filename wraps across rows so it stays fully visible; the button never
        gets pushed down. Returns a *list* of lines. (The click itself is
        hit-tested in :meth:`_on_mouse` — WheelScrollControl ignores per-fragment
        handlers.)"""
        label = self._zoom_label()
        lw = text_width(label)
        fill = "class:preview.header.focus" if focused else "class:preview.header"
        # the [+]/[-] button is white; when focused it keeps the header background
        # so it stays legible against the focus tint
        bstyle = ("class:preview.header.focus #ffffff bold" if focused
                  else "#ffffff bold")
        wrapped = self._wrap_frags(line, width - lw - 1, width)
        out = []
        for i, wl in enumerate(wrapped):
            used = sum(text_width(t) for _, t in wl)
            if i == 0:
                pad = max(1, width - used - lw)
                out.append(wl + [(fill, " " * pad), (bstyle, label)])
            else:
                pad = max(0, width - used)
                out.append(wl + ([(fill, " " * pad)] if pad else []))
        # Remember how many *characters* the button row holds: prompt_toolkit's
        # mouse handler reports a click's column as a character index, not a
        # display column, so a header with wide (CJK) characters can't be
        # hit-tested against the display width. The button is the trailing
        # three characters, so the click test keys off this count instead.
        self._btn_chars = sum(len(t) for _, t in out[0])
        return out

    @staticmethod
    def _flatten_lines(lines):
        out = []
        for ln in lines:
            out.extend(ln)
            out.append(("", "\n"))
        return out

    def _toggle_zoom(self):
        """The header's [+]/[-] button: zoom the preview pane, or un-zoom it."""
        if self.app.zoom and self.app.preview_focused():
            self.app.toggle_zoom()       # [-]: back to the even split
        else:
            self.focus()                 # make the preview the pane zoom enlarges
            if not self.app.zoom:
                self.app.toggle_zoom()   # [+]: zoom in
            else:
                self.app.invalidate()    # already zoomed (on the list): swap here

    def _current_identity(self):
        """A lightweight id of what's previewed, so scroll resets when it changes."""
        if self.app.mode == "gitlog":
            return ("gitlog", self.app.logview.current_hash())
        if self.app.mode == "git":
            e = self.app.gitview.current()
            return ("git", norm(e.path) if e else None)
        e = self.app.explorer.current()
        return ("file", norm(e.path) if e else None)

    def scroll(self, delta):
        self._scroll = max(0, self._scroll + delta)
        self.app.invalidate()

    def _visible_text(self):
        """Window the full preview to the visible slice, scrolled by ``_scroll``.

        The body is windowed the same way whether or not the pane is focused, so
        opening the shell (which unfocuses the pane) keeps the current scroll
        position rather than snapping to the top; a content change resets it via
        ``_scroll_id`` above, so list navigation still shows each file from top.
        Only the pinned header's focus tint depends on the focus state."""
        ident = self._current_identity()
        if ident != self._scroll_id:
            self._scroll_id = ident
            self._scroll = 0
        frags = self._text()
        lines = list(split_lines(frags))
        height = self._visible_height()
        # totals drive the scrollbar (shown whenever the content overflows)
        self._sb_total = len(lines)
        self._sb_view = height
        self._btn_chars = 0  # set by _line_with_button when the button is drawn
        focused = self.app.preview_focused()
        # keep the leading title lines (filename / meta) pinned — every builder
        # emits them before the first blank line — and scroll only the body.
        hdr = 0
        while hdr < len(lines) and any(t.strip() for _, t in lines[hdr]):
            hdr += 1
        header, body = lines[:hdr], lines[hdr:]
        body_h = max(1, height - hdr)
        max_scroll = max(0, len(body) - body_h)
        self._scroll = max(0, min(self._scroll, max_scroll))
        # tint the pinned header only while focused so the focus state reads at a
        # glance; the first header line always carries the [-]/[+] zoom button.
        width = self._content_width()
        if focused:
            hunk = self._current_hunk()
            if hunk is not None:
                selected_style = self._selected_hunk_style(hunk)
                for line_no in range(hunk["line"], min(hunk["end"], len(body))):
                    line = body[line_no]
                    body[line_no] = [
                        ((style + " " + selected_style).strip(), text)
                        for style, text in line
                    ]
                    used = sum(text_width(text) for _, text in line)
                    if used < width:
                        body[line_no].append(
                            (selected_style, " " * (width - used)))
        header_lines = []
        for i, ln in enumerate(header):
            if i == 0:
                if focused:
                    ln = [((s + " class:preview.header.focus").strip(), t)
                          for s, t in ln]
                header_lines.extend(self._line_with_button(ln, width, focused))
            elif focused:
                header_lines.append(self._focus_header(ln, width))
            else:
                header_lines.append(ln)
        shown = header_lines + body[self._scroll:self._scroll + body_h]
        return self._flatten_lines(shown)

    @staticmethod
    def _selected_hunk_style(hunk):
        return ("class:preview-hunk-staged-selected" if hunk["staged"]
                else "class:preview-hunk-selected")

    def _kb(self):
        kb = KeyBindings()

        @kb.add("down")
        @kb.add("j")
        def _(event):
            if not self.jump_hunk(1):
                self.scroll(1)

        @kb.add("up")
        @kb.add("k")
        def _(event):
            if not self.jump_hunk(-1):
                self.scroll(-1)

        @kb.add("u")
        def _(event):
            self.confirm_revert_hunk()

        @kb.add("s")
        def _(event):
            self.stage_current_hunk()

        @kb.add("pagedown")
        @kb.add(" ")
        def _(event):
            self.scroll(self._visible_height())

        @kb.add("pageup")
        def _(event):
            self.scroll(-self._visible_height())

        @kb.add("g")
        @kb.add("home")
        def _(event):
            self._scroll = 0
            self.app.invalidate()

        @kb.add("G")
        @kb.add("end")
        def _(event):
            self._scroll = 10 ** 9  # clamped to the bottom on the next render
            self.app.invalidate()

        # Esc backs a zoomed pane out to the even split first; otherwise (and
        # F7/F8, handled globally) it returns focus to the list.
        @kb.add("escape")
        def _(event):
            if self.app._zoom_active():
                self.app.toggle_zoom()
                return
            self.app.focus_active_list()

        # h/Shift+H step back to the list (the mirror of l/Shift+L stepping in).
        @kb.add("h")
        @kb.add("H")
        def _(event):
            self.app.focus_active_list()

        # ':' opens the shell here too — the key is caught by whichever control
        # holds focus, so without this a ':' typed while the preview is focused
        # would fall through unhandled (the explorer list binds it, the preview
        # didn't). Use the configured command key so a remap in nshrc still works.
        cmd_key = self.app.keys.get("command")
        if cmd_key:
            try:
                @kb.add(cmd_key)
                def _(event):
                    self.app.switch_mode("shell")
            except Exception:  # noqa: BLE001 - bad key spec in nshrc; skip it
                pass

        return kb

    # -- cache ----------------------------------------------------------------
    def clear(self):
        self._cache.clear()
        self._diff_hunks.clear()
        self._hunk_selection.clear()

    def _current_diff_key(self):
        if self.app.mode == "git":
            entry = self.app.gitview.current()
        elif self.app.mode == "explorer":
            current = self.app.explorer.current()
            entry = self._explorer_git_entry(current) if current else None
        else:
            entry = None
        return self._git_key(entry) if entry is not None else None

    def has_diff_hunks(self):
        return bool(self._diff_hunks.get(self._current_diff_key()))

    def jump_hunk(self, direction):
        key = self._current_diff_key()
        hunks = self._diff_hunks.get(key, ())
        if not hunks:
            return False
        current = self._hunk_selection.get(key, 0)
        current = max(0, min(len(hunks) - 1, current + direction))
        self._hunk_selection[key] = current
        self._scroll = hunks[current]["line"]
        self.app.invalidate()
        return True

    def _current_hunk(self):
        key = self._current_diff_key()
        hunks = self._diff_hunks.get(key, ())
        if not hunks:
            return None
        selected = max(0, min(len(hunks) - 1,
                              self._hunk_selection.get(key, 0)))
        return hunks[selected]

    def confirm_revert_hunk(self):
        path = self._current_preview_path()
        if path is not None and self._conflict_undo.get(norm(path)):
            self._undo_conflict(path)
            return
        hunk = self._current_hunk()
        if hunk is None:
            self.app.set_message("no change to revert")
            return
        self.app.confirm(
            f"Revert this change in '{hunk['name']}'?",
            lambda ok: self._do_revert_hunk(hunk, ok))

    def _do_revert_hunk(self, hunk, ok):
        # ConfirmDialog restores the mode's default focus (the left list) before
        # invoking its result callback. A hunk action originates in the preview,
        # so put focus back here for both confirmation and cancellation.
        self.focus()
        if not ok:
            self.app.set_message("change revert cancelled")
            return

        async def do():
            rc, out = await git.apply_hunk(
                hunk["patch"], hunk["cwd"], hunk["staged"])
            if rc:
                detail = out.strip().splitlines()[-1] if out.strip() else "git apply failed"
                self.app.set_message(f"cannot revert change: {detail}")
                return
            self.clear()
            self.app.set_message("change reverted")
            await self.app.refresh_git()
        asyncio.ensure_future(do())

    def _current_preview_path(self):
        if self.app.mode == "git":
            entry = self.app.gitview.current()
        elif self.app.mode == "explorer":
            entry = self.app.explorer.current()
        else:
            entry = None
        return entry.path if entry is not None else None

    def stage_current_hunk(self):
        hunk = self._current_hunk()
        if hunk is None:
            self.app.set_message("no change to stage")
            return
        if hunk.get("kind") == "conflict":
            self._open_conflict_menu(hunk)
            return
        # This action starts in the preview and has no dialog, so retain focus
        # while Git and the refreshed diff are loaded asynchronously.
        self.focus()

        async def do():
            rc, out = await git.stage_hunk(
                hunk["patch"], hunk["cwd"], hunk["staged"])
            if rc:
                detail = out.strip().splitlines()[-1] if out.strip() else "git apply failed"
                self.app.set_message(f"cannot stage change: {detail}")
                return
            action = "unstaged" if hunk["staged"] else "staged"
            self.clear()
            self.app.set_message(f"change {action}")
            await self.app.refresh_git()
            self.focus()
        asyncio.ensure_future(do())

    def _open_conflict_menu(self, hunk):
        self.app.open_menu("Resolve conflict", [
            ("Accept ours", lambda: self._resolve_conflict(hunk, "ours")),
            ("Accept theirs", lambda: self._resolve_conflict(hunk, "theirs")),
            ("Accept both", lambda: self._resolve_conflict(hunk, "both")),
        ], on_close=self.focus)

    def _resolve_conflict(self, hunk, choice):
        self.focus()

        async def do():
            try:
                content = await run_in_thread(hunk["path"].read_bytes)
                if hunk["original"] not in content:
                    self.app.set_message("conflict block changed; reload the preview")
                    return
                index_info = await git.conflict_index(hunk["path"], hunk["cwd"])
                replacement = hunk[choice]
                updated = content.replace(hunk["original"], replacement, 1)
                await run_in_thread(hunk["path"].write_bytes, updated)
                record = {**hunk, "replacement": replacement,
                          "content_before": content, "content_after": updated,
                          "index_info": index_info}
                self._conflict_undo.setdefault(norm(hunk["path"]), []).append(record)
                if b"<<<<<<<" not in updated:
                    rc, out = await git.stage_resolved_file(hunk["path"], hunk["cwd"])
                    if rc:
                        self.app.set_message(
                            "conflicts resolved but staging failed: " + out.strip())
                    else:
                        self.app.set_message("all conflicts resolved and file staged")
                else:
                    self.app.set_message(f"accepted {choice}")
                self.clear()
                await self.app.refresh_git()
                self.focus()
            except OSError as exc:
                self.app.set_message(f"cannot resolve conflict: {exc}")
        asyncio.ensure_future(do())

    def _undo_conflict(self, path):
        self.focus()
        records = self._conflict_undo.get(norm(path), [])
        if not records:
            self.app.set_message("no resolved conflict to restore")
            return
        record = records[-1]

        async def do():
            try:
                content = await run_in_thread(path.read_bytes)
                if content != record["content_after"]:
                    self.app.set_message("resolved block changed; cannot restore it")
                    return
                updated = record["content_before"]
                await run_in_thread(path.write_bytes, updated)
                if record["index_info"]:
                    rc, out = await git.restore_conflict_index(
                        path, record["cwd"], record["index_info"])
                    if rc:
                        self.app.set_message("cannot restore conflict index: " + out.strip())
                        return
                records.pop()
                self.clear()
                self.app.set_message("conflict block restored")
                await self.app.refresh_git()
                self.focus()
            except OSError as exc:
                self.app.set_message(f"cannot restore conflict: {exc}")
        asyncio.ensure_future(do())

    def _key(self, entry):
        try:
            st = entry.path.stat()
            return (norm(entry.path), st.st_mtime_ns, st.st_size)
        except OSError:
            return (norm(entry.path), 0, 0)

    # -- reactive text --------------------------------------------------------
    def _text(self):
        if self.app.mode == "gitlog":
            return self._log_text()
        if self.app.mode == "git":
            return self._git_text()
        entry = self.app.explorer.current()
        if entry is None:
            return [("class:preview.dim", " (nothing selected)")]
        git_entry = self._explorer_git_entry(entry)
        if git_entry is not None:
            key = self._git_key(git_entry)
            if key in self._cache:
                return self._cache[key]
            if key not in self._inflight:
                self._inflight.add(key)
                asyncio.ensure_future(self._load_git(git_entry, key))
            return [("class:preview.dim", " loading diff…")]
        key = self._key(entry)
        if key in self._cache:
            return self._cache[key]
        if key not in self._inflight:
            self._inflight.add(key)
            asyncio.ensure_future(self._load(entry, key))
        return [("class:preview.dim", " loading…")]

    def _explorer_git_entry(self, entry):
        """Adapt a tracked changed Explorer file to the Git diff preview."""
        if entry.is_dir:
            return None
        status = getattr(self.app.explorer, "git_status", None)
        context = status.direct_file_context(entry.path) if status else None
        code, repo_root = context if context is not None else (None, None)
        if code not in ("M", "S", "C"):
            return None
        try:
            rel = os.path.relpath(entry.path, repo_root).replace(os.sep, "/")
        except ValueError:
            rel = entry.name
        return SimpleNamespace(
            path=entry.path, code=code, rel=rel, git_cwd=repo_root)

    # -- git-mode diff preview ------------------------------------------------
    def _git_text(self):
        entry = self.app.gitview.current()
        if entry is None:
            return [("class:preview.dim", " (no changes)")]
        key = self._git_key(entry)
        if key in self._cache:
            return self._cache[key]
        if key not in self._inflight:
            self._inflight.add(key)
            asyncio.ensure_future(self._load_git(entry, key))
        return [("class:preview.dim", " loading diff…")]

    def _git_key(self, entry):
        try:
            st = entry.path.stat()
            return ("git", norm(entry.path), entry.code, st.st_mtime_ns, st.st_size)
        except OSError:
            return ("git", norm(entry.path), entry.code, 0, 0)

    # -- git-log commit preview ----------------------------------------------
    def _log_text(self):
        h = self.app.logview.current_hash()
        if not h:
            return [("class:preview.dim", " (no commit)")]
        key = ("gitlog", h)
        if key in self._cache:
            return self._cache[key]
        if key not in self._inflight:
            self._inflight.add(key)
            asyncio.ensure_future(self._load_commit(h, key))
        return [("class:preview.dim", " loading…")]

    async def _load_commit(self, h, key):
        try:
            text = await git.commit_show(h, self.app.cwd)
            frags = self._build_commit(text)
        except Exception as exc:  # noqa: BLE001 - shown in the pane
            frags = [("class:preview.dim", f" error: {exc}")]
        self._cache[key] = frags
        self._inflight.discard(key)
        self.app.invalidate()

    def _build_commit(self, text):
        # render git's own ANSI colours (commit header, the --stat histogram and
        # the diff are all coloured by git) rather than re-colouring by prefix,
        # which left the stat's +/- counts uncoloured.
        if not text:
            return [("class:preview.dim", " (no commit)")]
        frags = []
        for line in text.splitlines()[:MAX_LINES * 2]:
            frags += to_formatted_text(ANSI(line))
            frags.append(("", "\n"))
        return frags

    async def _load_git(self, entry, key):
        try:
            if entry.code == "?":  # untracked: git diff is empty, show the content
                frags = await run_in_thread(self._build_untracked, entry)
            elif entry.code == "C":
                cwd = getattr(entry, "git_cwd", self.app.cwd)
                content = await run_in_thread(entry.path.read_bytes)
                hunks, lines = self._parse_conflicts(content, entry, cwd)
                self._diff_hunks[key] = hunks
                self._hunk_selection[key] = 0
                frags = self._build_conflicts(entry, lines)
            else:
                cwd = getattr(entry, "git_cwd", self.app.cwd)
                (unstaged, staged), (unstaged_zero, staged_zero) = await asyncio.gather(
                    git.diff_parts(entry.path, cwd),
                    git.diff_parts(entry.path, cwd, unified=0))
                self._diff_hunks[key] = self._parse_hunks(
                    unstaged, staged, entry, cwd, unstaged_zero, staged_zero)
                self._hunk_selection[key] = 0
                frags = self._build_diff(entry, unstaged, staged)
        except Exception as exc:  # noqa: BLE001 - shown in the pane
            frags = [("class:preview.dim", f" diff error: {exc}")]
        self._cache[key] = frags
        self._inflight.discard(key)
        self.app.invalidate()

    @staticmethod
    def _parse_conflicts(content, entry, cwd):
        """Return selectable conflict blocks and the file's display lines."""
        raw_lines = content.splitlines(keepends=True)
        hunks = []
        i = 0
        while i < len(raw_lines):
            if not raw_lines[i].startswith(b"<<<<<<<"):
                i += 1
                continue
            start = i
            middle = base = end = None
            i += 1
            while i < len(raw_lines):
                if raw_lines[i].startswith(b"|||||||") and base is None:
                    base = i
                elif raw_lines[i].startswith(b"======="):
                    middle = i
                elif raw_lines[i].startswith(b">>>>>>>"):
                    end = i
                    break
                i += 1
            if middle is None or end is None:
                break
            ours_end = base if base is not None else middle
            original = b"".join(raw_lines[start:end + 1])
            ours = b"".join(raw_lines[start + 1:ours_end])
            theirs = b"".join(raw_lines[middle + 1:end])
            hunks.append({
                # blank body row + the "Conflicts" section title precede file.
                "line": start + 2, "end": end + 3,
                "kind": "conflict", "staged": False,
                "path": entry.path, "cwd": cwd, "name": entry.rel,
                "original": original, "ours": ours, "theirs": theirs,
                "both": ours + theirs,
            })
            i = end + 1
        display = [line.decode("utf-8", "replace").rstrip("\r\n")
                   for line in raw_lines]
        return hunks, display

    def _build_conflicts(self, entry, lines):
        frags = self._diff_header(entry)
        frags.append(("class:git.conflict bold", " ── Conflicts ──\n"))
        if not lines:
            return frags + [("class:preview.dim", " (empty file)\n")]
        for line in lines:
            if line.startswith(("<<<<<<<", "=======", ">>>>>>>", "|||||||")):
                style = "class:git.conflict bold"
            else:
                style = "class:preview"
            frags.append((style, _sanitize(line) + "\n"))
        return frags

    @staticmethod
    def _parse_hunks(unstaged, staged, entry, cwd,
                     unstaged_zero=None, staged_zero=None):
        """Map each contiguous +/- block to an independently applicable patch."""
        result = []
        # body line zero is the blank separator after the pinned file header.
        line_offset = 1
        sources = (
            (unstaged, unstaged if unstaged_zero is None else unstaged_zero, False),
            (staged, staged if staged_zero is None else staged_zero, True),
        )
        for text, zero_text, is_staged in sources:
            lines = text.splitlines()
            if not lines:
                continue
            # Each non-empty section has one visible Staged/Unstaged title.
            line_offset += 1
            patches = PreviewView._diff_patch_hunks(zero_text)
            blocks = []
            i = 0
            while i < len(lines):
                # ---/+++ are file headers, not deleted/added content.
                if (not lines[i].startswith(("+", "-"))
                        or lines[i].startswith(("+++ ", "--- "))):
                    i += 1
                    continue
                start = i
                i += 1
                while i < len(lines) and (
                        lines[i].startswith(("+", "-"))
                        or lines[i].startswith("\\ No newline")):
                    i += 1
                blocks.append((start, i))
            for (start, end), patch in zip(blocks, patches):
                result.append({
                    "line": line_offset + start,
                    "end": line_offset + end,
                    "patch": patch,
                    "staged": is_staged,
                    "cwd": cwd,
                    "name": entry.rel,
                })
            line_offset += len(lines)
        return result

    @staticmethod
    def _diff_patch_hunks(text):
        """Split a zero-context file diff into single-hunk patches."""
        lines = text.splitlines()
        header = []
        i = 0
        while i < len(lines) and not lines[i].startswith("@@"):
            header.append(lines[i])
            i += 1
        patches = []
        while i < len(lines):
            if not lines[i].startswith("@@"):
                i += 1
                continue
            start = i
            i += 1
            while i < len(lines) and not lines[i].startswith("@@"):
                i += 1
            patches.append("\n".join(header + lines[start:i]) + "\n")
        return patches

    _DIFF_LABEL = {"M": "modified", "S": "staged", "C": "conflict", "?": "untracked"}

    def _diff_header(self, entry):
        label = self._DIFF_LABEL.get(entry.code, "")
        return [("class:preview.header", f" {entry.rel}"),
                ("class:preview.dim", f"  [{label}]\n\n")]

    def _build_diff(self, entry, unstaged, staged=""):
        frags = self._diff_header(entry)
        if not unstaged and not staged:
            return frags + [("class:preview.dim", " (no diff)\n")]
        for title, text, title_style in (
                ("Unstaged changes", unstaged, "class:git.modified"),
                ("Staged changes", staged, "class:git.staged")):
            if not text:
                continue
            frags.append((title_style + " bold", f" ── {title} ──\n"))
            self._append_diff_lines(frags, text)
        return frags

    @staticmethod
    def _append_diff_lines(frags, text):
        for line in text.splitlines():
            if line.startswith("+"):
                style = "class:git.staged"     # additions: green
            elif line.startswith("-"):
                style = "class:shell.error"     # deletions: red
            elif line.startswith("@@"):
                style = "class:preview.meta"    # hunk header
            elif line.startswith(("diff ", "index ", "new file", "deleted file")):
                style = "class:preview.dim"
            else:
                style = "class:preview"
            frags.append((style, _sanitize(line) + "\n"))

    def _build_untracked(self, entry):
        try:
            with open(entry.path, "rb") as f:
                chunk = f.read(READ_BYTES)
        except OSError as exc:
            return [("class:preview.dim", f" cannot read: {exc}")]
        frags = self._diff_header(entry)
        if not chunk:
            return frags + [("class:preview.dim", " (empty file)\n")]
        if b"\x00" in chunk:
            return frags + [("class:preview.dim", " (binary file)\n")]
        text = self._decode(chunk)
        if text is None:
            return frags + [("class:preview.dim", " (binary file)\n")]
        for ln in text.splitlines()[:MAX_LINES]:
            frags.append(("class:git.staged", "+" + _sanitize(ln) + "\n"))  # all new
        if len(text.splitlines()) > MAX_LINES or len(chunk) >= READ_BYTES:
            frags.append(("class:preview.dim", "\n … (truncated)\n"))
        return frags

    async def _load(self, entry, key):
        try:
            frags = await run_in_thread(self._build, entry)
        except Exception as exc:  # noqa: BLE001 - shown in the pane
            frags = [("class:preview.dim", f" preview error: {exc}")]
        self._cache[key] = frags
        self._inflight.discard(key)
        self.app.invalidate()

    # -- builders (run in a worker thread) ------------------------------------
    def _build(self, entry):
        if entry.is_dir:
            return self._build_dir(entry)
        try:
            with open(entry.path, "rb") as f:
                chunk = f.read(READ_BYTES)
        except OSError as exc:
            return [("class:preview.dim", f" cannot read: {exc}")]
        if entry.is_image:
            return self._build_image(entry)
        if not chunk:
            return self._header(entry) + self._meta_line(entry, ["empty file"])
        if b"\x00" in chunk:
            return self._build_binary(entry, chunk)
        text = self._decode(chunk)
        if text is None:
            return self._build_binary(entry, chunk)
        return self._build_text(entry, text, truncated=len(chunk) >= READ_BYTES)

    @staticmethod
    def _header(entry):
        return [("class:preview.header", f" {entry.name}\n")]

    @staticmethod
    def _meta_line(entry, parts=()):
        """Header meta: for a symlink, a first line ``→ target`` with the path it
        points to; then a line of permissions (``ls -l`` style) · the given
        ``parts`` (e.g. line count, size) · the modified date. Falls back to just
        ``parts`` if the file can't be stat'd."""
        try:
            st = os.lstat(entry.path)
        except OSError:
            st = None
        out = []
        if st is not None and entry.is_link:
            try:
                target = os.readlink(entry.path)
            except OSError:
                target = "?"
            out.append(("class:preview.dim", "  →  "))
            out.append(("class:explorer.link", target + "\n"))
        if st is not None:
            out.append(("class:preview.meta", f" {stat.filemode(st.st_mode)}"))
        extras = list(parts)
        if st is not None:
            extras.append(datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"))
        if extras:
            prefix = "  ·  " if st is not None else " "
            out.append(("class:preview.dim", prefix + "  ·  ".join(extras)))
        out.append(("class:preview", "\n"))
        return out

    @staticmethod
    def _decode(chunk):
        for enc in ("utf-8", "cp949", "latin-1"):
            try:
                return chunk.decode(enc)
            except UnicodeDecodeError:
                continue
        return None

    def _build_dir(self, entry):
        items = model.list_dir(entry.path, self.app.explorer.show_hidden)
        frags = [("class:preview.header", f" {entry.name}/\n")]
        frags += self._meta_line(entry, [f"{len(items)} items"])
        frags.append(("class:preview", "\n"))
        for e in items[:MAX_DIR_ITEMS]:
            name = e.name + ("/" if e.is_dir else "")
            frags.append((config.entry_style(e), f" {config.entry_icon(e)} {name}\n"))
        if len(items) > MAX_DIR_ITEMS:
            frags.append(("class:preview.dim", f" … and {len(items) - MAX_DIR_ITEMS} more\n"))
        return frags

    def _build_image(self, entry):
        dims = image_dimensions(entry.path)
        parts = ["[image]", human_size(entry.size)]
        if dims:
            parts.append(f"{dims[0]}×{dims[1]} px")
        frags = self._header(entry) + self._meta_line(entry, parts)
        frags.append(("class:preview", "\n"))
        return frags

    def _build_text(self, entry, text, truncated):
        lines = text.splitlines()
        frags = self._header(entry) + self._meta_line(
            entry, [f"{len(lines)} lines", human_size(entry.size)])
        frags.append(("class:preview", "\n"))
        for ln in lines[:MAX_LINES]:
            frags.append(("class:preview", _sanitize(ln) + "\n"))
        if len(lines) > MAX_LINES or truncated:
            frags.append(("class:preview.dim", "\n … (truncated)\n"))
        return frags

    def _build_binary(self, entry, chunk):
        frags = self._header(entry) + self._meta_line(
            entry, ["[binary]", human_size(entry.size)])
        frags.append(("class:preview", "\n"))
        for off in range(0, min(len(chunk), HEX_BYTES), 16):
            row = chunk[off:off + 16]
            hexpart = " ".join(f"{b:02x}" for b in row)
            asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            frags.append(("class:preview", f" {off:08x}  {hexpart:<47}  |{asciipart}|\n"))
        return frags
