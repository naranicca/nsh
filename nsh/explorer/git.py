"""Asynchronous Git integration.

Everything here runs ``git`` through ``asyncio`` subprocesses so the UI thread
never blocks while status is computed for a large repository.
"""
import asyncio
import os
import sys
import stat as stat_module
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
    # normalised directory path -> aggregate status of changed descendants.
    # Git porcelain normally lists only the files, while Explorer renders their
    # parent directories too; this map lets those rows carry a marker.
    directories: Dict[str, str] = field(default_factory=dict)
    # immediate child repositories when the displayed cwd itself is not a repo;
    # values are RC (clean) or RD (dirty)
    child_repos: Dict[str, str] = field(default_factory=dict)
    # full file status for those child repositories, used when one is expanded
    child_statuses: Dict[str, object] = field(default_factory=dict)
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

    def code_for(self, path, include_descendants=True) -> Optional[str]:
        """Porcelain code for ``path`` — a direct match, else inherited from an
        untracked ancestor directory.

        git collapses an untracked directory into a single ``dir/`` entry, so a
        file inside it has no status line of its own — yet it *is* untracked.
        When the path isn't listed directly, walk up toward the repo root: if it
        sits under one of the untracked directories, report it as untracked
        ('?'). This is what makes the marker (and the Add action) show for files
        in a brand-new directory.

        ``include_descendants=False`` hides only the status aggregated from
        child files. Explorer uses it for expanded directories because those
        child markers are already visible."""
        code = self.files.get(norm(path))
        if code is not None:
            return code
        if include_descendants:
            code = self.directories.get(norm(path))
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

    def add_file(self, path, code):
        """Record a changed path and aggregate its status into repo parents."""
        path = Path(path)
        self.files[norm(path)] = code
        # Do not propagate untracked state to directories: a '?' beside a
        # folder misleadingly suggests that the whole folder is untracked.
        # Individual files still inherit '?' from Git's collapsed dir/ entry.
        if code == "?":
            return
        priority = {"?": 0, "S": 1, "M": 2, "C": 3}
        root_key = norm(self.root) if self.root is not None else None
        for parent in path.parents:
            key = norm(parent)
            current = self.directories.get(key)
            if current is None or priority.get(code, 0) > priority.get(current, 0):
                self.directories[key] = code
            if key == root_key:
                break

    def display_code(self, path, is_dir=False, expanded=False, is_parent=False):
        """Status marker for an Explorer row rather than an action target."""
        if is_parent:
            return None  # ``..`` is navigation, not a directory status row
        if not self.is_repo:
            key = norm(path)
            child = self.child_statuses.get(key)
            if child is not None:  # the child repository's own row
                return self.child_repos.get(key)
            # An expanded repository exposes its files in this Explorer tree.
            # Walk upward to find which immediate child repository owns the row.
            for parent in Path(path).parents:
                child = self.child_statuses.get(norm(parent))
                if child is not None:
                    return child.display_code(
                        path, is_dir=is_dir, expanded=expanded)
            return None
        code = self.code_for(path, include_descendants=not (is_dir and expanded))
        return None if is_dir and code == "?" else code

    def direct_file_code(self, path):
        """Direct file status in this repo or an expanded child repository."""
        context = self.direct_file_context(path)
        return context[0] if context is not None else None

    def direct_file_context(self, path):
        """Return ``(code, repository_root)`` for a directly changed file."""
        key = norm(path)
        if self.is_repo:
            code = self.files.get(key)
            return (code, self.root) if code is not None else None
        for parent in Path(path).parents:
            child = self.child_statuses.get(norm(parent))
            if child is not None:
                code = child.files.get(key)
                return (code, child.root) if code is not None else None
        return None


async def child_repositories(directories):
    """Return GitStatus objects for directories that are repository roots."""
    semaphore = asyncio.Semaphore(8)

    async def inspect(path):
        async with semaphore:
            # One unreadable directory must not fail the whole gather: this
            # walks every visible child of a non-repository directory, so it
            # meets system folders and other user's homes routinely. Those are
            # simply "not a repository".
            try:
                layout = _repository_layout(path)
                if layout is None or norm(layout[0]) != norm(path):
                    return None
                root, gitdir = layout
                rc, porcelain = await run_git(_STATUS_ARGS, path)
                if rc != 0:
                    return None
                status = GitStatus(is_repo=True, root=root)
                _parse_porcelain_v2(status, porcelain)
                status.has_remote = _has_remote(gitdir) or status.has_upstream
                status.in_progress = _operation_in_progress(gitdir)
            except OSError:
                return None
            return norm(path), status

    found = await asyncio.gather(*(inspect(Path(path)) for path in directories))
    return dict(item for item in found if item is not None)


_STATUS_ARGS = [
    "-c", "core.quotepath=false", "status",
    "--porcelain=v2", "--branch", "--show-stash",
]

def _stat_or_none(path):
    """``path.stat()``, or ``None`` when the path cannot be read.
    
    pathlib's ``is_dir`` / ``is_file`` / ``exists`` only swallow "it isn't
    there" errors (ENOENT, ENOTDIR, EBADF, ELOOP) - anything else, a directory
    the user may not stat above all, is re-raised. Explorer probes ``.git`` in
    whatever it happens to be listing, so ``/root/.git`` and friends have to
    read as "not a repository" rather than crash the app.
    """
    try:
        return path.stat()
    except (OSError, ValueError):
        return None


def _exists(path):
    """Permission-safe ``Path.exists()`` - see :func:`_stat_or_none`."""
    return _stat_or_none(path) is not None
 

def _exists(path):
    """Permission-safe ``Path.exists()`` - see :func:`_stat_or_none`."""
    return _stat_or_none(path) is not None
 
def _repository_layout(directory):
    """Return ``(worktree root, git dir)`` without starting ``git.exe``."""
    path = Path(directory).absolute()
    for root in (path, *path.parents):
        marker = root / ".git"
        info = _stat_or_none(marker)
        if info is None:  # missing, or not ours to look at
            continue
        if stat_module.S_ISDIR(info.st_mode):
            return root, marker
        if stat_module.S_ISREG(info.st_mode):
            try:
                line = marker.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if line.lower().startswith("gitdir:"):
                gitdir = Path(line.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = root / gitdir
                return root, gitdir.absolute()
    return None


def _common_gitdir(gitdir):
    try:
        value = (Path(gitdir) / "commondir").read_text(
            encoding="utf-8", errors="replace").strip()
    except OSError:
        return Path(gitdir)
    common = Path(value)
    return common if common.is_absolute() else (Path(gitdir) / common).absolute()


def metadata_signature(directory, child_directories=()):
    """Cheap fingerprint for Git state changes outside the worktree."""
    layouts = {}
    primary = _repository_layout(directory)
    candidates = ((directory,) if primary is not None
                  else (directory, *child_directories))
    for candidate in candidates:
        layout = primary if candidate == directory else _repository_layout(candidate)
        if layout is None:
            continue
        root, gitdir = layout
        # Descendants of the current repository share its metadata. Only add a
        # child layout when that child is itself a repository root.
        if candidate != directory and norm(root) != norm(candidate):
            continue
        layouts[norm(gitdir)] = (Path(root), Path(gitdir))

    signatures = []
    for _key, (root, gitdir) in sorted(layouts.items()):
        common = _common_gitdir(gitdir)
        candidates = {
            gitdir / "HEAD", gitdir / "index", gitdir / "MERGE_HEAD",
            gitdir / "CHERRY_PICK_HEAD", gitdir / "REVERT_HEAD",
            gitdir / "BISECT_LOG", common / "packed-refs",
            common / "refs" / "stash",
        }
        try:
            head = (gitdir / "HEAD").read_text(
                encoding="utf-8", errors="replace").strip()
        except OSError:
            head = ""
        if head.lower().startswith("ref:"):
            candidates.add(common / head.split(":", 1)[1].strip())
        for operation in ("rebase-apply", "rebase-merge", "sequencer"):
            candidates.add(gitdir / operation)

        files = []
        for path in sorted(candidates, key=lambda item: str(item)):
            try:
                stat_result = path.stat()
                value = (stat_result.st_mtime_ns, stat_result.st_size)
            except OSError:
                value = None
            files.append((str(path), value))
        signatures.append((norm(root), tuple(files)))
    return tuple(signatures)


def _has_remote(gitdir):
    """Best-effort remote detection from private/common Git config files."""
    candidates = {Path(gitdir) / "config", _common_gitdir(gitdir) / "config"}
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(line.strip().lower().startswith('[remote "')
               for line in text.splitlines()):
            return True
    return False


def _parse_porcelain_v2(status, output):
    """Populate branch metadata and file state from porcelain-v2 status."""
    for line in output.splitlines():
        if line.startswith("# branch.oid "):
            status.has_commits = line[13:].strip() != "(initial)"
        elif line.startswith("# branch.head "):
            branch = line[14:].strip()
            status.branch = "(detached)" if branch == "(detached)" else branch
        elif line.startswith("# branch.upstream "):
            status.has_upstream = bool(line[18:].strip())
        elif line.startswith("# branch.ab "):
            fields = line[12:].split()
            for field in fields:
                if field.startswith("+") and field[1:].isdigit():
                    status.ahead = int(field[1:])
                elif field.startswith("-") and field[1:].isdigit():
                    status.behind = int(field[1:])
        elif line.startswith("# stash "):
            value = line[8:].strip()
            status.has_stash = value.isdigit() and int(value) > 0
        elif line.startswith("? "):
            _add_v2_path(status, line[2:], "?", line[2:].endswith("/"))
        elif line.startswith("u "):
            fields = line.split(" ", 10)
            if len(fields) > 10:
                _add_v2_path(status, fields[10], "C")
        elif line.startswith("1 "):
            fields = line.split(" ", 8)
            if len(fields) <= 8:
                continue
            xy, path = fields[1], fields[8].split("\t", 1)[0]
            x, y = xy[0], xy[1]
            if "U" in xy:
                code = "C"
            elif x != ".":
                code = "S" if y == "." else "M"
            else:
                code = "M"
            _add_v2_path(status, path, code)
        elif line.startswith("2 "):
            fields = line.split(" ", 9)
            if len(fields) <= 9:
                continue
            xy, path = fields[1], fields[9].split("\t", 1)[0]
            x, y = xy[0], xy[1]
            if "U" in xy:
                code = "C"
            elif x != ".":
                code = "S" if y == "." else "M"
            else:
                code = "M"
            _add_v2_path(status, path, code)


def _add_v2_path(status, path, code, is_dir_entry=False):
    path = path.strip().strip('"')
    abspath = status.root / path.rstrip("/")
    status.add_file(abspath, code)
    status.entries.append((abspath, code))
    if code == "?" and is_dir_entry:
        status.untracked_dirs.add(norm(abspath))


def _parse_porcelain(status, porcelain):
    """Populate file status from ``git status --porcelain`` output."""
    if not porcelain:
        return
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:]
        if " -> " in path:  # rename: keep the destination
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        is_dir_entry = path.endswith("/")
        abspath = status.root / path.rstrip("/")
        x, y = code[0], code[1]
        if "U" in code or code in ("AA", "DD"):
            value = "C"
        elif code == "??":
            value = "?"
        elif x not in (" ", "?"):
            value = "S" if y == " " else "M"
        else:
            value = "M"
        status.add_file(abspath, value)
        status.entries.append((abspath, value))
        if value == "?" and is_dir_entry:
            status.untracked_dirs.add(norm(abspath))


async def run_git(args, cwd, env=None, input_data=None):
    """Run ``git <args>`` in ``cwd``; return ``(returncode, combined_output)``.

    ``env`` overrides the child environment (used by the scripted rebase below);
    ``None`` inherits nsh's own environment.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd),
            env=env,
            stdin=(asyncio.subprocess.PIPE if input_data is not None else None),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, NotADirectoryError, OSError):
        return 127, ""
    if isinstance(input_data, str):
        input_data = input_data.encode("utf-8")
    try:
        out, _ = await proc.communicate(input_data)
    except asyncio.CancelledError:
        # Cancelling a directory/status refresh must not leave git.exe running
        # after its owning pane has moved elsewhere.
        if proc.returncode is None:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                await proc.wait()
        raise
    return proc.returncode, out.decode("utf-8", "replace")


async def _out(args, cwd):
    rc, out = await run_git(args, cwd)
    return out if rc == 0 else None


_QUERY_TASKS = {}


async def query(directory, child_directories=()) -> GitStatus:
    """Share concurrent status requests for the same pane/repository scope."""
    key = (norm(directory), tuple(sorted(norm(path)
                                         for path in child_directories)))
    entry = _QUERY_TASKS.get(key)
    if entry is None:
        entry = {
            "task": asyncio.create_task(
                _query_uncached(directory, child_directories)),
            "waiters": 0,
        }
        _QUERY_TASKS[key] = entry
    entry["waiters"] += 1
    try:
        return await asyncio.shield(entry["task"])
    finally:
        entry["waiters"] -= 1
        if entry["waiters"] == 0:
            if not entry["task"].done():
                entry["task"].cancel()
                try:
                    await entry["task"]
                except asyncio.CancelledError:
                    pass
            if _QUERY_TASKS.get(key) is entry:
                del _QUERY_TASKS[key]


async def _query_uncached(directory, child_directories=()) -> GitStatus:
    """Detect the repo, current branch and per-file status for ``directory``."""
    st = GitStatus()
    layout = _repository_layout(directory)
    if layout is None:
        st.child_statuses = await child_repositories(child_directories)
        st.child_repos = {
            path: "RD" if status.dirty else "RC"
            for path, status in st.child_statuses.items()
        }
        return st
    st.root, gitdir = layout
    st.is_repo = True
    rc, output = await run_git(_STATUS_ARGS, directory)
    if rc == 0:
        _parse_porcelain_v2(st, output)
    st.has_remote = _has_remote(gitdir) or st.has_upstream
    st.in_progress = _operation_in_progress(gitdir)
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
    unstaged, staged = await diff_parts(path, cwd)
    return (unstaged + staged).strip()


async def diff_parts(path, cwd, unified=None):
    """Return ``(unstaged, staged)`` diff text separately for hunk actions."""
    context = [] if unified is None else [f"--unified={unified}"]
    _, unstaged = await run_git(
        ["-c", "color.ui=never", "diff"] + context + ["--", str(path)], cwd
    )
    _, staged = await run_git(
        ["-c", "color.ui=never", "diff", "--cached"] + context
        + ["--", str(path)], cwd
    )
    return unstaged, staged


async def apply_hunk(patch, cwd, staged=False):
    """Reverse one unified-diff hunk, including the index when it is staged."""
    # Selection patches come from ``git diff --unified=0``. Git deliberately
    # rejects zero-context patches unless --unidiff-zero is explicit.
    base = ["apply", "--reverse", "--unidiff-zero", "--whitespace=nowarn"]
    if not staged:
        return await run_git(base + ["-"], cwd, input_data=patch)
    rc, out = await run_git(base + ["--cached", "-"], cwd, input_data=patch)
    if rc:
        return rc, out
    rc, out = await run_git(base + ["-"], cwd, input_data=patch)
    if rc:
        await run_git(["apply", "--unidiff-zero", "--whitespace=nowarn",
                       "--cached", "-"], cwd,
                      input_data=patch)
    return rc, out


async def stage_hunk(patch, cwd, staged=False):
    """Stage one change block, or unstage it when it is already staged."""
    args = ["apply", "--cached", "--unidiff-zero", "--whitespace=nowarn"]
    if staged:
        args.append("--reverse")
    return await run_git(args + ["-"], cwd, input_data=patch)


async def conflict_index(path, cwd):
    """Capture the unmerged index stages so a resolved block can be undone."""
    rc, out = await run_git(
        ["ls-files", "-u", "--", str(path)], cwd)
    return out if rc == 0 else ""


async def stage_resolved_file(path, cwd):
    return await run_git(["add", "--", str(path)], cwd)


async def restore_conflict_index(path, cwd, index_info):
    """Replace a stage-0 entry with its saved stage 1/2/3 conflict entries."""
    rc, out = await run_git(["update-index", "--force-remove", "--", str(path)], cwd)
    if rc:
        return rc, out
    return await run_git(
        ["update-index", "--index-info"], cwd, input_data=index_info)


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


async def log_graph(cwd, paths=None):
    """Raw ANSI-coloured history, optionally limited to selected file paths."""
    args = ["-c", "core.quotepath=false", "log", "--graph",
            "--color=always", "--pretty=" + _LOG_FORMAT]
    if paths:
        args += ["--", *(str(path) for path in paths)]
    return await _out(args, cwd) or ""


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
    out = await _out(["status", "--porcelain", "untracked-files=no"], cwd)
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
def _operation_in_progress(gitdir):
    """Which multi-step operation (if any) is mid-flight: a marker file in the
    git dir tells us — used to offer Continue/Abort and conflict resolution."""
    if gitdir is None:
        return None
    g = Path(gitdir)
    # a readable .git can still hold unreadable entries, so probe permissively
    if _exists(g / "rebase-merge") or _exists(g / "rebase-apply"):
        return "rebase"
    if _exists(g / "MERGE_HEAD"):
        return "merge"
    if _exists(g / "CHERRY_PICK_HEAD"):
        return "cherry-pick"
    if _exists(g / "REVERT_HEAD"):
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
