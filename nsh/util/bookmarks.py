"""Persistent directory bookmarks, stored one path per line in
``~/.config/nsh/bookmarks``.
"""
from pathlib import Path

from ..config import config_dir
from .paths import norm


class Bookmarks:
    def __init__(self):
        self._paths = []  # absolute path strings, in insertion order
        self.load()

    def path(self) -> Path:
        return config_dir() / "bookmarks"

    def load(self):
        self._paths = []
        try:
            text = self.path().read_text(encoding="utf-8")
        except (OSError, ValueError):
            return
        seen = set()
        for line in text.splitlines():
            p = line.strip()
            if p and norm(p) not in seen:
                seen.add(norm(p))
                self._paths.append(p)

    def save(self):
        try:
            p = self.path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("".join(line + "\n" for line in self._paths), encoding="utf-8")
        except OSError:
            pass

    def list(self):
        return list(self._paths)

    def contains(self, path) -> bool:
        key = norm(path)
        return any(norm(p) == key for p in self._paths)

    def add(self, path):
        path = str(Path(path))
        if not self.contains(path):
            self._paths.append(path)
            self.save()

    def remove(self, path):
        key = norm(path)
        kept = [p for p in self._paths if norm(p) != key]
        if len(kept) != len(self._paths):
            self._paths = kept
            self.save()

    def toggle(self, path) -> bool:
        """Add if absent / remove if present; return True when now bookmarked."""
        if self.contains(path):
            self.remove(path)
            return False
        self.add(path)
        return True
