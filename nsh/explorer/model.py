"""Directory listing model."""
import os
from dataclasses import dataclass
from pathlib import Path

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


def _is_exec(dir_entry, name: str) -> bool:
    if os.name == "nt":
        return os.path.splitext(name)[1].lower() in WIN_EXEC_EXTS
    try:
        return dir_entry.is_file() and os.access(dir_entry.path, os.X_OK)
    except OSError:
        return False


def list_dir(path, show_hidden: bool = False):
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
            try:
                size = 0 if is_dir else de.stat(follow_symlinks=False).st_size
            except OSError:
                size = 0
            entries.append(
                Entry(
                    path=Path(de.path),
                    name=name,
                    is_dir=is_dir,
                    is_link=is_link,
                    is_exec=(not is_dir) and _is_exec(de, name),
                    is_image=(not is_dir) and ext in IMAGE_EXTS,
                    size=size,
                )
            )
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries
