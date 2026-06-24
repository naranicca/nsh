"""Fuzzy subsequence matching and directory indexing for search mode.

``match`` is a small fzf-style scorer: the query must appear as a (case-
insensitive) subsequence of the candidate, with bonuses for consecutive runs
and word-boundary starts so that ``aps`` ranks ``app/search.py`` above an
incidental scatter of those letters. ``gather`` walks a directory tree once,
pruning the usual heavy/uninteresting directories.
"""
import os

WORD_BOUNDARY = set("/\\._- ")

# The default set of directories skipped when indexing: VCS metadata, dependency
# trees and tool caches — pure noise. Build-output dirs (build/, dist/) are
# deliberately NOT here: they hold real artifacts you may want to find (an
# executable, a bundle). This is the default for the nshrc `search_exclude`
# setting, which the user can edit to add or remove names.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".tox", ".idea", ".vscode",
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
    # locate the basename, ignoring a directory's trailing separator so its real
    # name (not an empty string) gets the basename bonus
    base = t.rstrip("/\\")
    base_start = max(base.rfind("/"), base.rfind("\\")) + 1
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


def gather(root, show_hidden=False, limit=50000, skip=None):
    """Relative paths of files and directories under ``root`` (directories get a
    trailing separator), gathered **breadth-first** and capped at ``limit``.

    Breadth-first matters: a depth-first walk dives fully into the first subtree,
    so one huge directory (e.g. Windows' ``AppData``) exhausts ``limit`` before
    any sibling like ``source/`` is reached. Breadth-first indexes every branch
    at a shallow depth first, so nearby paths show up even in a giant home dir.

    ``skip`` is the set of directory names to prune; ``None`` falls back to the
    built-in :data:`SKIP_DIRS`. The search view passes the user's nshrc
    ``search_exclude`` list, which *replaces* the default — so a name can be
    removed (to search it) as well as added.
    """
    skip = SKIP_DIRS if skip is None else set(skip)
    items = []
    root = os.fspath(root)
    queue = [root]
    while queue:
        current = queue.pop(0)
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            continue
        for e in entries:
            name = e.name
            if not show_hidden and name.startswith("."):
                continue
            try:
                is_dir = e.is_dir()
            except OSError:
                is_dir = False
            rel = os.path.relpath(e.path, root)
            if is_dir:
                if name in skip:
                    continue
                items.append(rel + os.sep)
                queue.append(e.path)
            else:
                items.append(rel)
            if len(items) >= limit:
                return items
    return items
