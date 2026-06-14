"""Side preview pane.

Renders the entry under the explorer cursor: a directory's contents, a text
file's body (encoding auto-detected), an image's metadata (dimensions parsed
straight from the file header — no Pillow dependency), or a hexdump for binary
files. Reads happen in a worker thread and results are cached per path, so
scrolling the listing never blocks the UI.
"""
import asyncio
import struct

from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl

from .. import config
from ..util.paths import human_size, norm
from . import model

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


class PreviewView:
    def __init__(self, app):
        self.app = app
        self._cache = {}
        self._inflight = set()
        self.control = FormattedTextControl(self._text, focusable=False)
        self.window = Window(self.control, wrap_lines=True, style="class:preview")

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

    async def _load(self, entry, key):
        try:
            frags = await asyncio.to_thread(self._build, entry)
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
            return self._header(entry) + [("class:preview.dim", " (empty file)\n")]
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
    def _decode(chunk):
        for enc in ("utf-8", "cp949", "latin-1"):
            try:
                return chunk.decode(enc)
            except UnicodeDecodeError:
                continue
        return None

    def _build_dir(self, entry):
        items = model.list_dir(entry.path, self.app.explorer.show_hidden)
        frags = [
            ("class:preview.header", f" {entry.name}/  "),
            ("class:preview.dim", f"({len(items)} items)\n\n"),
        ]
        for e in items[:MAX_DIR_ITEMS]:
            name = e.name + ("/" if e.is_dir else "")
            frags.append((config.entry_style(e), f" {config.entry_icon(e)} {name}\n"))
        if len(items) > MAX_DIR_ITEMS:
            frags.append(("class:preview.dim", f" … and {len(items) - MAX_DIR_ITEMS} more\n"))
        return frags

    def _build_image(self, entry):
        dims = image_dimensions(entry.path)
        frags = self._header(entry) + [
            ("class:preview.meta", " [image] "),
            ("class:preview.dim", human_size(entry.size)),
        ]
        if dims:
            frags.append(("class:preview.dim", f"  ·  {dims[0]}×{dims[1]} px"))
        frags.append(("class:preview", "\n"))
        return frags

    def _build_text(self, entry, text, truncated):
        lines = text.splitlines()
        frags = self._header(entry) + [
            ("class:preview.dim", f" {len(lines)} lines · {human_size(entry.size)}\n\n"),
        ]
        for ln in lines[:MAX_LINES]:
            frags.append(("class:preview", _sanitize(ln) + "\n"))
        if len(lines) > MAX_LINES or truncated:
            frags.append(("class:preview.dim", "\n … (truncated)\n"))
        return frags

    def _build_binary(self, entry, chunk):
        frags = self._header(entry) + [
            ("class:preview.meta", " [binary] "),
            ("class:preview.dim", f"{human_size(entry.size)}\n\n"),
        ]
        for off in range(0, min(len(chunk), HEX_BYTES), 16):
            row = chunk[off:off + 16]
            hexpart = " ".join(f"{b:02x}" for b in row)
            asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            frags.append(("class:preview", f" {off:08x}  {hexpart:<47}  |{asciipart}|\n"))
        return frags
