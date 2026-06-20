"""Persistent multi-line notes, stored as a JSON list of strings in
``~/.config/nsh/notes.json`` (newest first).
"""
import json
from pathlib import Path

from ..config import config_dir


class Notes:
    def __init__(self):
        self._notes = []  # list[str], newest first
        self.load()

    def path(self) -> Path:
        return config_dir() / "notes.json"

    def load(self):
        self._notes = []
        try:
            data = json.loads(self.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, list):
            self._notes = [str(n) for n in data if str(n).strip()]

    def save(self):
        try:
            p = self.path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._notes, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        except OSError:
            pass

    def list(self):
        return list(self._notes)

    def __len__(self):
        return len(self._notes)

    def add(self, text):
        """Add a note at the top (newest first); returns its index (0)."""
        text = text.strip()
        if not text:
            return None
        self._notes.insert(0, text)
        self.save()
        return 0

    def delete(self, index):
        """Remove and return the note at ``index`` (or None if out of range)."""
        if 0 <= index < len(self._notes):
            removed = self._notes.pop(index)
            self.save()
            return removed
        return None

    def insert(self, index, text):
        """Re-insert ``text`` at ``index`` (used to undo a delete)."""
        index = max(0, min(index, len(self._notes)))
        self._notes.insert(index, text)
        self.save()

    def replace(self, index, text):
        """Replace the note at ``index`` with ``text`` (used when editing)."""
        text = text.strip()
        if 0 <= index < len(self._notes) and text:
            self._notes[index] = text
            self.save()
