"""Cross-platform path helpers.

All path manipulation goes through :mod:`pathlib` so Windows ``\\`` and POSIX
``/`` separators are handled uniformly.
"""
import os
from pathlib import Path


def shorten_home(path) -> str:
    """Render ``path`` with ``$HOME`` collapsed to ``~`` (display only)."""
    p = Path(path)
    try:
        home = Path.home()
    except RuntimeError:
        return str(p)
    if p == home:
        return "~"
    try:
        return "~" + os.sep + str(p.relative_to(home))
    except ValueError:
        return str(p)


def human_size(n: int) -> str:
    """Human-readable byte count, e.g. ``4.0K``."""
    f = float(n)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if f < 1024 or unit == "P":
            return f"{int(f)}{unit}" if unit == "B" else f"{f:.1f}{unit}"
        f /= 1024
    return f"{int(n)}B"


def norm(path) -> str:
    """Canonical comparison key for a path.

    Uses ``abspath`` (lexical, no symlink resolution) plus ``normcase`` so that
    paths coming from ``git`` and from directory scans compare equal, including
    on case-insensitive Windows filesystems.
    """
    return os.path.normcase(os.path.abspath(os.fspath(path)))
