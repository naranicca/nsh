"""Asynchronous Git integration.

Everything here runs ``git`` through ``asyncio`` subprocesses so the UI thread
never blocks while status is computed for a large repository.
"""
import asyncio
import os
import sys
import tempfile
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
    # normcased abspaths of untracked directories. git collapses an untracked
    # directory into a single "dir/" entry, so files inside it never appear in
    # ``files``; code_for() consults this set so they still inherit the '?' marker
    untracked_dirs: set = field(default_factory=set)
    behind: int = 0  # commits the upstream has that we don't
    ahead: int = 0   # commits we have that the upstream doesn't
    has_upstream: bool = False  # the branch has a configured @{upstream}
    has_remote: bool = False    # at least one git remote is configured
    has_commits: bool = False   # HEAD points at a commit (not an unborn branch)
    has_stash: bool = False     # the stash stack is non-empty
    in_progress: Optional[str] = None  # "merge"/"rebase"/"cherry-pick"/"revert" or None

    @property
    def dirty(self) -> bool:
        """True when there are tracked changes to commit (untracked files excluded)."""
        return any(code != "?" for code in self.files.values())

    @property
    def can_push(self) -> bool:
        """True when pushing is meaningful: there are commits ahead of the
        upstream, or — when no upstream is set yet — a remote exists and the
        branch has commits (the push will set the upstream)."""
        if self.ahead > 0:
            return True
        return (self.has_remote and not self.has_upstream and self.has_commits
                and self.branch not in (None, "(detached)"))

    @property
    def can_pull(self) -> bool:
        """True when there is an upstream branch to pull from. (Not gated on the
        behind count, which is stale until a fetch — pulling is how you find out
        about new upstream commits.)"""
        return self.has_upstream

    def code_for(self, path) -> Optional[str]:
        """Porcelain code for ``path`` — a direct match, else inherited from an
        untracked ancestor directory.

        git collapses an untracked directory into a single ``dir/`` entry, so a
        file inside it has no status line of its own — yet it *is* untracked.
        When the path isn't listed directly, walk up toward the repo root: if it
        sits under one of the untracked directories, report it as untracked
        ('?'). This is what makes the marker (and the Add action) show for files
        in a brand-new directory."""
        code = self.files.get(norm(path))
        if code is not None:
            return code
        if self.untracked_dirs:
            root_key = norm(self.root) if self.root is not None else None
            for parent in Path(path).parents:
                pk = norm(parent)
                if pk == root_key:
                    break
                if pk in self.untracked_dirs:
                    return "?"
        return None


async def run_git(args, cwd, env=None):
    """Run ``git <args>`` in ``cwd``; return ``(returncode, combined_output)``.

    ``env`` overrides the child environment (used by the scripted rebase below);
    ``None`` inherits nsh's own environment.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd),
            env=env,
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
    # @{upstream} only resolves when a tracking branch is configured
    st.has_upstream = counts is not None
    remotes = await _out(["remote"], directory)
    st.has_remote = bool(remotes and remotes.strip())
    st.has_commits = await _out(["rev-parse", "--verify", "-q", "HEAD"], directory) is not None
    stash = await _out(["stash", "list"], directory)
    st.has_stash = bool(stash and stash.strip())
    st.in_progress = await _operation_in_progress(st.root)

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
            is_dir_entry = path.endswith("/")  # git marks an untracked dir "dir/"
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
            if c == "?" and is_dir_entry:
                st.untracked_dirs.add(norm(abspath))
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
    """Return ``(local, remote, current)`` branch names.

    Remote symbolic refs such as ``origin/HEAD`` are omitted; they are aliases,
    not branches a user should check out directly.
    """
    local_out = await _out(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd)
    remote_out = await _out(
        ["for-each-ref", "--format=%(refname:short)%00%(symref)",
         "refs/remotes"], cwd)
    branches = ([ln.strip() for ln in local_out.splitlines() if ln.strip()]
                if local_out is not None else [])
    remotes = []
    if remote_out is not None:
        for line in remote_out.splitlines():
            ref, _nul, symbolic_target = line.partition("\0")
            if ref.strip() and not symbolic_target.strip():
                remotes.append(ref.strip())
    cur = await _out(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    cur = cur.strip() if cur else None
    return branches, remotes, cur


async def checkout_branch(name, cwd):
    """Switch to an existing branch (``git checkout <name>``)."""
    return await run_git(["checkout", name], cwd)


async def checkout_remote_branch(ref, cwd):
    """Check out ``remote/branch``, creating its tracking local branch.

    If the conventional local name already exists, switch to it rather than
    failing ``checkout --track`` because the branch has already been created.
    """
    local = ref.split("/", 1)[1] if "/" in ref else ref
    exists = await _out(["show-ref", "--verify", "refs/heads/" + local], cwd)
    if exists is not None:
        return await checkout_branch(local, cwd)
    return await run_git(["checkout", "--track", ref], cwd)


# -- read-only tree browsing (the branch "Browse" dialog) --------------------
async def ls_tree(rev, path, cwd):
    """The immediate children of directory ``path`` in ``rev`` (a branch, tag or
    commit); ``path`` is '' for the tree root. Returns ``[(name, fullpath,
    is_dir)]`` sorted directories-first then by name, or ``None`` if the tree /
    path can't be read. ``fullpath`` is repo-root-relative (what ``git show``
    and a recursive ls-tree expect)."""
    # --full-tree: list from the repo root regardless of cwd, and report paths
    # repo-root-relative (what show_bytes / a recursive ls-tree then expect)
    args = ["-c", "core.quotepath=false", "ls-tree", "--full-tree", "-z", rev]
    if path:
        args.append(path.rstrip("/") + "/")
    rc, out = await run_git(args, cwd)
    if rc != 0:
        return None
    entries = []
    for rec in out.split("\0"):
        if not rec:
            continue
        meta, _, name = rec.partition("\t")
        if not name:
            continue
        parts = meta.split()
        is_dir = len(parts) >= 2 and parts[1] == "tree"
        full = name.rstrip("/")
        base = full.rsplit("/", 1)[-1]
        entries.append((base, full, is_dir))
    entries.sort(key=lambda e: (not e[2], e[0].lower()))
    return entries


async def ls_tree_files(rev, path, cwd):
    """Every blob path (recursive) under directory ``path`` in ``rev`` — the
    files to extract when copying a whole directory out of a branch."""
    args = ["-c", "core.quotepath=false", "ls-tree", "--full-tree", "-r", "-z",
            "--name-only", rev]
    if path:
        args.append(path.rstrip("/") + "/")
    rc, out = await run_git(args, cwd)
    if rc != 0:
        return []
    return [p for p in out.split("\0") if p]


async def show_bytes(rev, path, cwd):
    """Raw bytes of the blob ``path`` at ``rev`` (``git show <rev>:<path>``),
    read binary-safe so images and other non-text files copy out intact.
    Returns ``(returncode, data)``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "show", f"{rev}:{path}",
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, NotADirectoryError, OSError):
        return 127, b""
    out, _ = await proc.communicate()
    return proc.returncode, out


async def delete_local_branch(name, cwd):
    """Delete a local branch (safe: ``git branch -d`` refuses unmerged work)."""
    return await run_git(["branch", "-d", name], cwd)

# Deleting a *remote* branch (git push --delete) contacts the server and may
# prompt for credentials, so it must run on a real terminal rather than through
# the piped run_git here; the explorer does that via the shell runner's
# run_in_term. (No helper here, to avoid a function that would hang on a prompt.)


# -- history view & commit editing -------------------------------------------
# Each commit line is "<graph art>\x00<full hash>\x00<coloured oneline>"; graph-
# only lines (merges) have no \x00. The view splits on \x00 to get the hash.
_LOG_FORMAT = ("tformat:%x00%H%x00%C(auto)%h%C(reset)%C(auto)%d%C(reset) "
               "%s %C(dim)(%cr) %C(blue)%an%C(reset)")


async def log_graph(cwd):
    """Raw ``git log --graph`` text (ANSI-coloured) for the history view."""
    return await _out(["-c", "core.quotepath=false", "log", "--graph",
                       "--color=always", "--pretty=" + _LOG_FORMAT], cwd) or ""


async def commit_show(commit, cwd):
    """The full detail + diff of one commit (ANSI-coloured), for the preview pane.

    ``--color=always`` keeps git's own colours — including the ``--stat``
    histogram (``+``/``-`` counts) that a prefix-based colouriser would miss."""
    rc, out = await run_git(
        ["-c", "core.quotepath=false", "show", "--color=always", "--stat", "-p",
         commit], cwd)
    return out if rc == 0 else ""


async def checkout_commit(commit, cwd):
    """Check out a commit (detached HEAD)."""
    return await run_git(["checkout", commit], cwd)


async def reset_hard(commit, cwd):
    """Roll the current branch back to ``commit`` (``git reset --hard``)."""
    return await run_git(["reset", "--hard", commit], cwd)


async def _has_parent(commit, cwd):
    rc, _ = await run_git(["rev-parse", "--verify", "-q", commit + "~1"], cwd)
    return rc == 0


async def rebase_base(commit, cwd):
    """Base ref for a rebase that includes ``commit`` and every commit after it:
    ``<commit>~1``, or ``--root`` when it is the repository's first commit."""
    return commit + "~1" if await _has_parent(commit, cwd) else "--root"


# Editors driving a non-interactive rebase: one rewrites the todo list, the
# other supplies the new message. Invoked as "<python> <script> <file>"; the
# message is passed through the NSH_REBASE_MSG environment variable. They must
# use forward slashes — git runs them through its shell, which mangles
# backslashes on Windows.
_SEQ_SCRIPT = (
    "import sys\n"
    "p = sys.argv[1]\n"
    "lines = open(p, encoding='utf-8').read().splitlines()\n"
    "out, done = [], False\n"
    "for ln in lines:\n"
    "    if not done and ln.startswith('pick '):\n"
    "        ln = 'reword ' + ln[5:]; done = True\n"
    "    out.append(ln)\n"
    "open(p, 'w', encoding='utf-8').write('\\n'.join(out) + '\\n')\n"
)
_MSG_SCRIPT = (
    "import os, sys\n"
    "open(sys.argv[1], 'w', encoding='utf-8').write(os.environ['NSH_REBASE_MSG'])\n"
)


def _rebase_env(message):
    """Environment + helper-script paths for a scripted, non-interactive rebase."""
    tmp = Path(tempfile.gettempdir())
    seq = tmp / "nsh_rebase_seq.py"
    msg = tmp / "nsh_rebase_msg.py"
    seq.write_text(_SEQ_SCRIPT, encoding="utf-8")
    msg.write_text(_MSG_SCRIPT, encoding="utf-8")
    py = sys.executable.replace("\\", "/")
    sp = str(seq).replace("\\", "/")
    mp = str(msg).replace("\\", "/")
    env = dict(os.environ)
    env["GIT_SEQUENCE_EDITOR"] = f'"{py}" "{sp}"'
    env["GIT_EDITOR"] = f'"{py}" "{mp}"'
    env["NSH_REBASE_MSG"] = message
    return env


async def reword(commit, message, cwd):
    """Change just ``commit``'s message (log only), via a scripted rebase that
    marks it ``reword`` and feeds the new message — no tree change, so it never
    conflicts. Rewrites history from ``commit`` onward."""
    env = _rebase_env(message)
    return await run_git(
        ["rebase", "-i", await rebase_base(commit, cwd)], cwd, env=env)


async def squash_onto(commit, message, cwd):
    """Combine ``commit`` and every commit after it (up to HEAD) into a single
    commit with ``message`` — soft-reset to its parent, then commit."""
    if not await _has_parent(commit, cwd):
        return 1, "cannot squash the root commit"
    rc, out = await run_git(["reset", "--soft", commit + "~1"], cwd)
    if rc != 0:
        return rc, out
    return await run_git(["commit", "-m", message], cwd)


async def is_clean(cwd):
    """True when the working tree has no changes (safe to rebase / squash)."""
    out = await _out(["status", "--porcelain"], cwd)
    return not (out and out.strip())


async def commit_subject(commit, cwd):
    """The one-line subject of ``commit`` (to prefill the reword dialog)."""
    out = await _out(["log", "-1", "--format=%s", commit], cwd)
    return out.strip() if out else ""


# -- stash --------------------------------------------------------------------
async def stash_push(cwd, message=None):
    """Stash the tracked changes (``git stash push``)."""
    args = ["stash", "push"]
    if message:
        args += ["-m", message]
    return await run_git(args, cwd)


async def stash_list(cwd):
    """Return the stash stack as ``[(ref, description)]`` (newest first)."""
    out = await _out(["stash", "list"], cwd)
    entries = []
    for line in (out or "").splitlines():
        ref = line.split(":", 1)[0].strip()
        if ref:
            entries.append((ref, line.strip()))
    return entries


async def stash_pop(cwd, ref=None):
    return await run_git(["stash", "pop"] + ([ref] if ref else []), cwd)


async def stash_apply(cwd, ref=None):
    return await run_git(["stash", "apply"] + ([ref] if ref else []), cwd)


async def stash_drop(cwd, ref):
    return await run_git(["stash", "drop", ref], cwd)


# -- merge/rebase conflicts ---------------------------------------------------
async def _operation_in_progress(root):
    """Which multi-step operation (if any) is mid-flight: a marker file in the
    git dir tells us — used to offer Continue/Abort and conflict resolution."""
    if root is None:
        return None
    gitdir = await _out(["rev-parse", "--git-dir"], root)
    if not gitdir:
        return None
    g = Path(gitdir.strip())
    if not g.is_absolute():
        g = Path(root) / g
    if (g / "rebase-merge").exists() or (g / "rebase-apply").exists():
        return "rebase"
    if (g / "MERGE_HEAD").exists():
        return "merge"
    if (g / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick"
    if (g / "REVERT_HEAD").exists():
        return "revert"
    return None


async def conflicted_files(cwd):
    """Paths with unresolved merge conflicts (unmerged in the index)."""
    out = await _out(
        ["-c", "core.quotepath=false", "diff", "--name-only", "--diff-filter=U"], cwd)
    return [ln.strip() for ln in (out or "").splitlines() if ln.strip()]


async def abort_operation(cwd, op):
    """Abort the in-progress ``op`` (rebase/merge/cherry-pick/revert)."""
    if op == "rebase":
        return await run_git(["rebase", "--abort"], cwd)
    if op == "cherry-pick":
        return await run_git(["cherry-pick", "--abort"], cwd)
    if op == "revert":
        return await run_git(["revert", "--abort"], cwd)
    return await run_git(["merge", "--abort"], cwd)
