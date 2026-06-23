"""A tiny persistent UI-state store (``~/.cache/nsh/state.json``).

For runtime preferences toggled from within the UI — not things the user edits
in ``nshrc`` — that should survive a restart, e.g. whether the process view shows
the full command or just the process name. Everything degrades to defaults if the
file is missing or unreadable.
"""
import json

from ..config import cache_dir


def _path():
    return cache_dir() / "state.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get(key, default=None):
    return _load().get(key, default)


def set(key, value):
    """Persist ``key`` = ``value`` (best-effort; a write failure is ignored)."""
    data = _load()
    if data.get(key) == value:
        return  # nothing changed -> no write
    data[key] = value
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass
