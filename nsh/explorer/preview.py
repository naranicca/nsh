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
        self._scroll = 0          # top visible line when the pane is focused
        self._scroll_id = None    # identity of what's scrolled (reset on change)
        self._sb_total = 0        # full document line count (for the scrollbar)
        self._sb_view = 0         # visible line count (for the scrollbar)
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
        if (mouse_event.position.y == 0
                and mouse_event.position.x >= self._width() - 3):
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
    def _truncate_frags(line, width):
        """Truncate a fragment line to ``width`` columns, preserving per-fragment
        styles. Returns ``(fragments, used_width)``."""
        out, used = [], 0
        for frag in line:
            if used >= width:
                break
            seg = cut_to_width(frag[1], width - used)
            if seg:
                out.append((frag[0], seg))
                used += text_width(seg)
        return out, used

    def _line_with_button(self, line, width, focused):
        """Append a right-aligned [+]/[-] zoom button to a header line, pinned to
        the pane's top-right corner. The header text is truncated so the line
        never wraps and push the button onto a second row. (The click itself is
        hit-tested in :meth:`_on_mouse` — WheelScrollControl ignores per-fragment
        handlers.)"""
        label = self._zoom_label()
        lw = text_width(label)
        shown, used = self._truncate_frags(line, max(0, width - lw - 1))
        pad = max(1, width - used - lw)
        fill = "class:preview.header.focus" if focused else "class:preview.header"
        bstyle = "class:preview.header.focus" if focused else "class:preview.meta"
        return shown + [(fill, " " * pad), (bstyle, label)]

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
        """Window the full preview to the visible slice while the pane is focused
        (so the arrows scroll it); rendered from the top as before otherwise."""
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
        if not self.app.preview_focused():
            self._scroll = 0
            width = self._width()
            if lines:  # add the [+] zoom button to the (first) header line
                lines = [self._line_with_button(lines[0], width, False)] + lines[1:]
            return self._flatten_lines(lines)
        # keep the leading title lines (filename / meta) pinned — every builder
        # emits them before the first blank line — and scroll only the body.
        hdr = 0
        while hdr < len(lines) and any(t.strip() for _, t in lines[hdr]):
            hdr += 1
        header, body = lines[:hdr], lines[hdr:]
        body_h = max(1, height - hdr)
        max_scroll = max(0, len(body) - body_h)
        self._scroll = max(0, min(self._scroll, max_scroll))
        # tint the pinned header so the focused pane is obvious; the first header
        # line also carries the right-aligned [-]/[+] zoom button
        width = self._width()
        header_lines = []
        for i, ln in enumerate(header):
            if i == 0:
                styled = [((s + " class:preview.header.focus").strip(), t)
                          for s, t in ln]
                header_lines.append(self._line_with_button(styled, width, True))
            else:
                header_lines.append(self._focus_header(ln, width))
        shown = header_lines + body[self._scroll:self._scroll + body_h]
        return self._flatten_lines(shown)

    def _kb(self):
        kb = KeyBindings()

        @kb.add("down")
        @kb.add("j")
        def _(event):
            self.scroll(1)

        @kb.add("up")
        @kb.add("k")
        def _(event):
            self.scroll(-1)

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

        return kb

    # -- cache ----------------------------------------------------------------
    def clear(self):
        self._cache.clear()

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
        key = self._key(entry)
        if key in self._cache:
            return self._cache[key]
        if key not in self._inflight:
            self._inflight.add(key)
            asyncio.ensure_future(self._load(entry, key))
        return [("class:preview.dim", " loading…")]

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
            else:
                text = await git.diff(entry.path, self.app.cwd)
                frags = self._build_diff(entry, text)
        except Exception as exc:  # noqa: BLE001 - shown in the pane
            frags = [("class:preview.dim", f" diff error: {exc}")]
        self._cache[key] = frags
        self._inflight.discard(key)
        self.app.invalidate()

    _DIFF_LABEL = {"M": "modified", "S": "staged", "C": "conflict", "?": "untracked"}

    def _diff_header(self, entry):
        label = self._DIFF_LABEL.get(entry.code, "")
        return [("class:preview.header", f" {entry.rel}"),
                ("class:preview.dim", f"  [{label}]\n\n")]

    def _build_diff(self, entry, text):
        frags = self._diff_header(entry)
        if not text:
            return frags + [("class:preview.dim", " (no diff)\n")]
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
        return frags

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
