"""Asynchronous Git integration.

Everything here runs ``git`` through ``asyncio`` subprocesses so the UI thread
never blocks while status is computed for a large repository.
"""
import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from ..util.paths import norm


@dataclass
class GitStatus:
    is_repo: bool = False
    branch: Optional[str] = None
    root: Optional[Path] = None
    # normalised abspath -> porcelain code in {"M","S","?","C"}
    files: Dict[str, str] = field(default_factory=dict)
    # changed files in original case/order: [(abspath Path, code)] — used by git
    # mode, which displays real paths (``files`` keys are normcased for matching)
    entries: list = field(default_factory=list)
    behind: int = 0  # commits the upstream has that we don't
    ahead: int = 0   # commits we have that the upstream doesn't

    @property
    def dirty(self) -> bool:
        """True when there are tracked changes to commit (untracked files excluded)."""
        return any(code != "?" for code in self.files.values())


async def run_git(args, cwd):
    """Run ``git <args>`` in ``cwd``; return ``(returncode, combined_output)``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, NotADirectoryError, OSError):
        return 127, ""
    out, _ = await proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace")


async def _out(args, cwd):
    rc, out = await run_git(args, cwd)
    return out if rc == 0 else None


async def query(directory) -> GitStatus:
    """Detect the repo, current branch and per-file status for ``directory``."""
    st = GitStatus()
    root = await _out(["rev-parse", "--show-toplevel"], directory)
    if root is None:
        return st
    st.is_repo = True
    st.root = Path(root.strip())

    branch = await _out(["rev-parse", "--abbrev-ref", "HEAD"], directory)
    if branch is not None:
        b = branch.strip()
        st.branch = b if b and b != "HEAD" else "(detached)"

    # behind/ahead vs. the upstream (absent if there's no tracking branch)
    counts = await _out(
        ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"], directory
    )
    if counts:
        parts = counts.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            st.behind, st.ahead = int(parts[0]), int(parts[1])

    # core.quotepath=false keeps CJK / unicode filenames intact in the output.
    porcelain = await _out(
        ["-c", "core.quotepath=false", "status", "--porcelain"], directory
    )
    if porcelain:
        for line in porcelain.splitlines():
            if len(line) < 4:
                continue
            code, path = line[:2], line[3:]
            if " -> " in path:  # rename: keep the destination
                path = path.split(" -> ", 1)[1]
            path = path.strip().strip('"')
            abspath = st.root / path.rstrip("/")
            x, y = code[0], code[1]
            if "U" in code or code in ("AA", "DD"):
                c = "C"
            elif code == "??":
                c = "?"
            elif x not in (" ", "?"):
                c = "S" if y == " " else "M"  # staged vs. staged+modified
            else:
                c = "M"
            st.files[norm(abspath)] = c
            st.entries.append((abspath, c))
    return st


async def stage_toggle(path, status: GitStatus, cwd):
    """Stage ``path``, or unstage it when it is already fully staged."""
    code = status.files.get(norm(path))
    if code == "S":
        return await run_git(["reset", "-q", "HEAD", "--", str(path)], cwd)
    return await run_git(["add", "--", str(path)], cwd)


async def revert(path, cwd):
    """Discard a tracked file's changes, restoring it to HEAD.

    ``git checkout HEAD -- <path>`` resets both the index and the working tree
    for ``path``, so any staged *and* unstaged changes are dropped.
    """
    return await run_git(["checkout", "HEAD", "--", str(path)], cwd)


async def diff(path, cwd):
    """Return the combined unstaged + staged diff text for ``path``."""
    _, unstaged = await run_git(
        ["-c", "color.ui=never", "diff", "--", str(path)], cwd
    )
    _, staged = await run_git(
        ["-c", "color.ui=never", "diff", "--cached", "--", str(path)], cwd
    )
    return (unstaged + staged).strip()


async def add_paths(paths, cwd):
    """Stage the given paths, so an explicitly selected untracked file can be
    committed by pathspec (``git commit -- <path>`` rejects untracked files)."""
    return await run_git(["add", "--"] + [str(p) for p in paths], cwd)


async def commit(message, cwd, paths=None):
    """Commit by pathspec, like the original nsh's ``git commit <files|.>``:
    take the current contents of ``paths`` regardless of what's staged. With no
    ``paths`` it commits the staged index instead."""
    args = ["commit", "-m", message]
    if paths:
        args += ["--"] + [str(p) for p in paths]
    return await run_git(args, cwd)


async def create_branch(name, cwd):
    """Create branch ``name`` and switch to it (``git checkout -b``)."""
    return await run_git(["checkout", "-b", name], cwd)


async def list_branches(cwd):
    """Return ``(branches, current)`` — local branch names and the checked-out one."""
    out = await _out(["branch", "--format=%(refname:short)"], cwd)
    if out is None:
        return [], None
    branches = [ln.strip() for ln in out.splitlines() if ln.strip()]
    cur = await _out(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    cur = cur.strip() if cur else None
    return branches, cur


async def checkout_branch(name, cwd):
    """Switch to an existing branch (``git checkout <name>``)."""
    return await run_git(["checkout", name], cwd)


async def delete_local_branch(name, cwd):
    """Delete a local branch (safe: ``git branch -d`` refuses unmerged work)."""
    return await run_git(["branch", "-d", name], cwd)

# Deleting a *remote* branch (git push --delete) contacts the server and may
# prompt for credentials, so it must run on a real terminal rather than through
# the piped run_git here; the explorer does that via the shell runner's
# run_in_term. (No helper here, to avoid a function that would hang on a prompt.)
