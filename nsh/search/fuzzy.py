"""Fuzzy subsequence matching and directory indexing for search mode.

``match`` is a small fzf-style scorer: the query must appear as a (case-
insensitive) subsequence of the candidate, with bonuses for consecutive runs
and word-boundary starts so that ``aps`` ranks ``app/search.py`` above an
incidental scatter of those letters. ``gather`` walks a directory tree once,
pruning the usual heavy/uninteresting directories.
"""
import os

WORD_BOUNDARY = set("/\\._- ")

# Directories never worth indexing for a file picker.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".tox", ".idea", ".vscode", "dist", "build",
}


def match(query, text):
    """Return ``(score, positions)`` if ``query`` matches, else ``None``.

    ``positions`` are indices into ``text`` of the matched characters (for
    highlighting). An empty query matches everything with score 0.
    """
    if not query:
        return (0.0, ())
    q = query.lower()
    t = text.lower()
    base_start = max(t.rfind("/"), t.rfind("\\")) + 1
    positions = []
    score = 0.0
    prev = -2
    start = 0
    for qc in q:
        idx = t.find(qc, start)
        if idx == -1:
            return None
        if idx == prev + 1:
            score += 10.0         # consecutive characters (contiguous run)
        if idx == 0 or t[idx - 1] in WORD_BOUNDARY:
            score += 8.0          # start of a path segment / word
        if idx >= base_start:
            score += 3.0          # within the basename
        positions.append(idx)
        prev = idx
        start = idx + 1
    if positions[0] == 0:
        score += 15.0             # anchored at the very start
    score -= len(text) * 0.05     # prefer shorter paths
    return (score, tuple(positions))


def search(query, items, limit=500):
    """Rank ``items`` (strings) against ``query``; return ``[(text, score, positions)]``."""
    out = []
    for it in items:
        m = match(query, it)
        if m is not None:
            out.append((it, m[0], m[1]))
    if query:
        out.sort(key=lambda r: r[1], reverse=True)
    return out[:limit]


def gather(root, show_hidden=False, limit=50000):
    """Walk ``root`` and return relative paths of files and directories.

    Directories carry a trailing separator so the caller can tell them apart.
    Pruned at ``limit`` entries to stay responsive on huge trees.
    """
    items = []
    root = os.fspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and (show_hidden or not d.startswith("."))
        ]
        for d in dirnames:
            rel = os.path.relpath(os.path.join(dirpath, d), root)
            items.append(rel + os.sep)
            if len(items) >= limit:
                return items
        for name in filenames:
            if not show_hidden and name.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            items.append(rel)
            if len(items) >= limit:
                return items
    return items
