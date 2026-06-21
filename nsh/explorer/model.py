"""Directory listing model."""
import os
import re
from dataclasses import dataclass
from pathlib import Path

_DIGITS_RE = re.compile(r"(\d+)")


def natural_key(name: str):
    """Sort key for natural/human ordering: ``2.txt`` sorts before ``10.txt``.

    Splits into alternating text / number runs and compares numbers by value.
    re.split always yields text at even indices and digits at odd ones, so two
    keys compare element-wise without mixing ``str`` and ``int``.
    """
    parts = _DIGITS_RE.split(name.lower())
    return [int(p) if p.isdigit() else p for p in parts]

IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".svg", ".ico", ".tif", ".tiff", ".heic",
}
# Treated as "executable" on Windows (no x-bit there).
WIN_EXEC_EXTS = {".exe", ".bat", ".cmd", ".com", ".ps1", ".msi"}


@dataclass
class Entry:
    path: Path
    name: str
    is_dir: bool
    is_link: bool
    is_exec: bool
    is_image: bool
    size: int
    mtime: int = 0  # st_mtime_ns (files only); used for change detection
    depth: int = 0  # indentation level when the listing is shown as a tree
    is_parent: bool = False  # the synthetic ".." row (go to the parent dir)


def _is_exec(dir_entry, name: str) -> bool:
    if os.name == "nt":
        return os.path.splitext(name)[1].lower() in WIN_EXEC_EXTS
    try:
        return dir_entry.is_file() and os.access(dir_entry.path, os.X_OK)
    except OSError:
        return False


def is_text_file(path, probe: int = 4096) -> bool:
    """Best-effort guess of whether ``path`` is an editable text file.

    Reads a small head: a NUL byte means binary, otherwise we try the same
    encodings the preview pane uses. Empty files count as text (editable).
    """
    try:
        with open(path, "rb") as f:
            chunk = f.read(probe)
    except OSError:
        return False
    if not chunk:
        return True
    if b"\x00" in chunk:
        return False
    for enc in ("utf-8", "cp949", "latin-1"):
        try:
            chunk.decode(enc)
            return True
        except UnicodeDecodeError:
            continue
    return False


# sort modes -> a key over an Entry (each falls back to the name for ties)
SORT_KEYS = {
    "name": lambda e: natural_key(e.name),
    "size": lambda e: (e.size, natural_key(e.name)),
    "date": lambda e: (e.mtime, natural_key(e.name)),
    "type": lambda e: (os.path.splitext(e.name)[1].lower(), natural_key(e.name)),
}


def sort_entries(entries, sort="name", reverse=False):
    """Sort in place by ``sort`` (a key in :data:`SORT_KEYS`), directories
    always first. ``reverse`` flips the order within each group."""
    key = SORT_KEYS.get(sort, SORT_KEYS["name"])
    entries.sort(key=key, reverse=reverse)
    entries.sort(key=lambda e: not e.is_dir)  # stable: dirs first, order kept
    return entries


def list_dir(path, show_hidden: bool = False, sort: str = "name", reverse: bool = False):
    """Return a sorted list of :class:`Entry` for ``path`` (dirs first)."""
    entries = []
    try:
        scan = os.scandir(path)
    except OSError:
        return entries
    with scan:
        for de in scan:
            name = de.name
            if not show_hidden and name.startswith("."):
                continue
            try:
                is_link = de.is_symlink()
                is_dir = de.is_dir()
            except OSError:
                is_link, is_dir = False, False
            ext = os.path.splitext(name)[1].lower()
            size = mtime = 0
            try:  # stat every entry for the modified time (dirs included, for date sort)
                stat = de.stat(follow_symlinks=False)
                mtime = stat.st_mtime_ns
                if not is_dir:
                    size = stat.st_size
            except OSError:
                pass
            entries.append(
                Entry(
                    path=Path(de.path),
                    name=name,
                    is_dir=is_dir,
                    is_link=is_link,
                    is_exec=(not is_dir) and _is_exec(de, name),
                    is_image=(not is_dir) and ext in IMAGE_EXTS,
                    size=size,
                    mtime=mtime,
                )
            )
    return sort_entries(entries, sort, reverse)
