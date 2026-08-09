import unittest
import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh import config
from nsh.app import NshApp
from nsh.explorer import git
from nsh.explorer.git import GitStatus
from nsh.explorer.preview import PreviewView
from nsh.explorer.view import ExplorerView


class GitStatusTests(unittest.TestCase):
    def test_concurrent_identical_git_queries_share_one_task(self):
        calls = 0
        release = asyncio.Event()

        async def uncached(directory, child_directories=()):
            nonlocal calls
            calls += 1
            await release.wait()
            return GitStatus(root=Path(directory))

        async def scenario():
            with mock.patch("nsh.explorer.git._query_uncached",
                            side_effect=uncached):
                first = asyncio.create_task(git.query("repo", ("repo/a",)))
                second = asyncio.create_task(git.query("repo", ("repo/a",)))
                await asyncio.sleep(0)
                release.set()
                return await asyncio.gather(first, second)

        results = asyncio.run(scenario())

        self.assertEqual(calls, 1)
        self.assertIs(results[0], results[1])

    def test_cancelled_git_process_is_terminated(self):
        started = asyncio.Event()

        class Process:
            returncode = None

            def __init__(self):
                self.terminated = False

            async def communicate(self, input_data):
                started.set()
                await asyncio.Event().wait()

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            async def wait(self):
                return self.returncode

        async def scenario():
            process = Process()
            with mock.patch("asyncio.create_subprocess_exec",
                            return_value=process):
                task = asyncio.create_task(git.run_git(["status"], Path(".")))
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            return process

        process = asyncio.run(scenario())
        self.assertTrue(process.terminated)

    @unittest.skipUnless(shutil.which("git"), "git executable required")
    def test_zero_context_change_patch_can_be_reverted(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "nsh@test.invalid"],
                           cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "nsh test"],
                           cwd=repo, check=True)
            path = repo / "sample.txt"
            path.write_text("one\ntwo\nthree\n", encoding="utf-8")
            subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"],
                           cwd=repo, check=True)
            path.write_text("one\nchanged\nthree\n", encoding="utf-8")

            unstaged, _ = asyncio.run(git.diff_parts(path, repo, unified=0))
            rc, output = asyncio.run(git.apply_hunk(unstaged, repo))

            self.assertEqual(rc, 0, output)
            self.assertEqual(path.read_text(encoding="utf-8"),
                             "one\ntwo\nthree\n")

    @unittest.skipUnless(shutil.which("git"), "git executable required")
    def test_zero_context_change_patch_can_be_staged_and_unstaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "nsh@test.invalid"],
                           cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "nsh test"],
                           cwd=repo, check=True)
            path = repo / "sample.txt"
            path.write_text("one\ntwo\nthree\n", encoding="utf-8")
            subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"],
                           cwd=repo, check=True)
            path.write_text("one\nchanged\nthree\nfour\naltered\n", encoding="utf-8")
            unstaged, _ = asyncio.run(git.diff_parts(path, repo, unified=0))
            first_patch = PreviewView._diff_patch_hunks(unstaged)[0]

            rc, output = asyncio.run(git.stage_hunk(first_patch, repo))
            self.assertEqual(rc, 0, output)
            remaining, staged = asyncio.run(git.diff_parts(path, repo, unified=0))
            self.assertIn("+altered", remaining)
            self.assertNotIn("+changed", remaining)
            self.assertIn("+changed", staged)
            self.assertNotIn("+altered", staged)

            rc, output = asyncio.run(git.stage_hunk(staged, repo, staged=True))
            self.assertEqual(rc, 0, output)
            remaining, staged = asyncio.run(git.diff_parts(path, repo, unified=0))
            self.assertIn("+changed", remaining)
            self.assertIn("+altered", remaining)
            self.assertEqual(staged, "")

    @unittest.skipUnless(shutil.which("git"), "git executable required")
    def test_resolved_file_can_restore_its_conflict_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "nsh@test.invalid"],
                           cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "nsh test"],
                           cwd=repo, check=True)
            path = repo / "sample.txt"
            path.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            subprocess.run(["git", "checkout", "-qb", "topic"], cwd=repo, check=True)
            path.write_text("theirs\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "theirs"], cwd=repo, check=True)
            subprocess.run(["git", "checkout", "-q", "-"], cwd=repo, check=True)
            path.write_text("ours\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "ours"], cwd=repo, check=True)
            subprocess.run(["git", "merge", "topic"], cwd=repo,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            index_info = asyncio.run(git.conflict_index(path, repo))
            self.assertIn(" 2\t", index_info)
            self.assertIn(" 3\t", index_info)
            path.write_text("ours\n", encoding="utf-8")
            rc, output = asyncio.run(git.stage_resolved_file(path, repo))
            self.assertEqual(rc, 0, output)
            self.assertEqual(asyncio.run(git.conflict_index(path, repo)), "")

            rc, output = asyncio.run(
                git.restore_conflict_index(path, repo, index_info))
            self.assertEqual(rc, 0, output)
            self.assertIn(" 2\t", asyncio.run(git.conflict_index(path, repo)))

    def test_modified_file_marks_all_parent_directories_modified(self):
        root = Path("repo").resolve()
        status = GitStatus(is_repo=True, root=root)

        status.add_file(root / "src" / "pkg" / "module.py", "M")

        self.assertEqual(status.code_for(root / "src" / "pkg"), "M")
        self.assertEqual(status.code_for(root / "src"), "M")

    def test_stronger_descendant_status_wins_for_directory(self):
        root = Path("repo").resolve()
        status = GitStatus(is_repo=True, root=root)
        directory = root / "src"

        status.add_file(directory / "staged.py", "S")
        status.add_file(directory / "modified.py", "M")

        self.assertEqual(status.code_for(directory), "M")

    def test_expanded_directory_can_hide_descendant_aggregate(self):
        root = Path("repo").resolve()
        status = GitStatus(is_repo=True, root=root)
        directory = root / "src"
        status.add_file(directory / "modified.py", "M")

        self.assertEqual(status.code_for(directory), "M")
        self.assertIsNone(status.code_for(directory, include_descendants=False))

    def test_untracked_files_do_not_mark_parent_directories(self):
        root = Path("repo").resolve()
        status = GitStatus(is_repo=True, root=root)
        directory = root / "new"
        file_path = directory / "draft.txt"
        status.add_file(file_path, "?")

        self.assertIsNone(status.code_for(directory))
        self.assertEqual(status.code_for(file_path), "?")

    def test_collapsed_untracked_directory_marker_is_hidden(self):
        root = Path("repo").resolve()
        status = GitStatus(is_repo=True, root=root)
        directory = root / "new"
        status.add_file(directory, "?")
        status.untracked_dirs.add(str(directory))

        self.assertEqual(status.code_for(directory), "?")
        self.assertIsNone(status.display_code(directory, is_dir=True))

    def test_parent_navigation_row_never_shows_git_status(self):
        root = Path("repo").resolve()
        status = GitStatus(is_repo=True, root=root)
        status.add_file(root / "a" / "b.txt", "M")

        self.assertEqual(status.code_for(root / "a"), "M")
        self.assertIsNone(status.display_code(
            root / "a", is_dir=True, is_parent=True))

    def test_child_repository_uses_blank_clean_or_dirty_marker(self):
        parent = Path("work").resolve()
        clean = parent / "clean"
        dirty = parent / "dirty"
        clean_status = GitStatus(is_repo=True, root=clean)
        dirty_status = GitStatus(is_repo=True, root=dirty)
        dirty_status.add_file(dirty / "changed.txt", "M")
        status = GitStatus(
            child_repos={str(clean).lower(): "RC", str(dirty).lower(): "RD"},
            child_statuses={str(clean).lower(): clean_status,
                            str(dirty).lower(): dirty_status})

        self.assertEqual(status.display_code(clean, is_dir=True), "RC")
        self.assertEqual(status.display_code(dirty, is_dir=True), "RD")
        self.assertEqual(config.GIT_SYMBOL["RC"], " ")
        self.assertEqual(config.GIT_SYMBOL["RD"], " ")

    def test_expanded_dirty_child_repo_shows_file_marks(self):
        parent = Path("work").resolve()
        repo = parent / "project"
        changed = repo / "src" / "app.py"
        child = GitStatus(is_repo=True, root=repo)
        child.add_file(changed, "M")
        status = GitStatus(
            child_repos={str(repo).lower(): "RD"},
            child_statuses={str(repo).lower(): child})

        self.assertEqual(status.display_code(
            repo, is_dir=True, expanded=True), "RD")
        self.assertEqual(status.display_code(changed), "M")
        self.assertEqual(status.display_code(repo / "src", is_dir=True), "M")

    def test_untracked_only_child_repository_is_clean_summary(self):
        repo = Path("work/project").resolve()
        child = GitStatus(is_repo=True, root=repo)
        child.add_file(repo / "draft.txt", "?")

        summary = "RD" if child.dirty else "RC"

        self.assertEqual(summary, "RC")

    def test_explorer_does_not_require_current_directory_to_be_a_repo(self):
        parent = Path("work").resolve()
        child = parent / "project"
        view = object.__new__(ExplorerView)
        child_status = GitStatus(is_repo=True, root=child)
        view.git_status = GitStatus(
            child_repos={str(child).lower(): "RC"},
            child_statuses={str(child).lower(): child_status})
        view.expanded = set()
        entry = SimpleNamespace(
            path=child, is_dir=True, is_parent=False)

        self.assertEqual(view._entry_git_code(entry), "RC")

    def test_child_repo_colour_is_not_reversed_on_cursor_row(self):
        clean_style = config.GIT_STYLE["RC"]
        dirty_style = config.GIT_STYLE["RD"]

        self.assertEqual(ExplorerView._git_marker_style(
            "RC", clean_style, "class:explorer.dir", True), clean_style)
        self.assertEqual(ExplorerView._git_marker_style(
            "RD", dirty_style, "class:explorer.dir", True), dirty_style)
        self.assertIn("reverse", ExplorerView._git_marker_style(
            "M", config.GIT_STYLE["M"], "class:explorer.file", True))

    def test_child_repository_scan_marks_clean_and_dirty_roots(self):
        parent = Path("work").resolve()
        clean = parent / "clean"
        dirty = parent / "dirty"
        nested = parent / "nested"

        def layout(path):
            return ((parent, parent / ".git") if path == nested
                    else (path, path / ".git"))

        async def output(_args, cwd, **_kwargs):
            body = ("1 .M N... 100644 100644 100644 a b changed.txt\n"
                    if cwd == dirty else "")
            return 0, "# branch.oid abc\n# branch.head main\n" + body

        with mock.patch("nsh.explorer.git._repository_layout",
                        side_effect=layout), \
                mock.patch("nsh.explorer.git.run_git", side_effect=output):
            found = asyncio.run(git.child_repositories((clean, dirty, nested)))

        self.assertFalse(found[str(clean).lower()].files)
        self.assertTrue(found[str(dirty).lower()].files)
        self.assertNotIn(str(nested).lower(), found)

    def test_query_uses_one_porcelain_v2_git_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text(
                '[remote "origin"]\n  url = example\n', encoding="utf-8")
            sub = root / "src"
            sub.mkdir()
            output = "\n".join([
                "# branch.oid abc123",
                "# branch.head topic",
                "# branch.upstream origin/topic",
                "# branch.ab +2 -3",
                "# stash 1",
                "1 .M N... 100644 100644 100644 a b src/changed.txt",
                "1 M. N... 100644 100644 100644 a b staged.txt",
                "2 R. N... 100644 100644 100644 a b R100 renamed.txt\told.txt",
                "u UU N... 100644 100644 100644 100644 a b c conflict.txt",
                "? new/",
            ])
            runner = mock.AsyncMock(return_value=(0, output))

            with mock.patch("nsh.explorer.git.run_git", runner):
                status = asyncio.run(git.query(sub))

        runner.assert_awaited_once_with(git._STATUS_ARGS, sub)
        self.assertTrue(status.is_repo)
        self.assertEqual(status.root, root)
        self.assertEqual(status.branch, "topic")
        self.assertEqual((status.ahead, status.behind), (2, 3))
        self.assertTrue(status.has_upstream)
        self.assertTrue(status.has_remote)
        self.assertTrue(status.has_commits)
        self.assertTrue(status.has_stash)
        self.assertEqual(status.code_for(root / "src" / "changed.txt"), "M")
        self.assertEqual(status.code_for(root / "staged.txt"), "S")
        self.assertEqual(status.code_for(root / "renamed.txt"), "S")
        self.assertEqual(status.code_for(root / "conflict.txt"), "C")
        self.assertIn(str(root / "new").lower(), status.untracked_dirs)

    def test_metadata_signature_changes_with_index_and_current_branch_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gitdir = root / ".git"
            branch = gitdir / "refs" / "heads" / "main"
            branch.parent.mkdir(parents=True)
            (gitdir / "HEAD").write_text(
                "ref: refs/heads/main\n", encoding="utf-8")
            (gitdir / "index").write_bytes(b"one")
            branch.write_text("first\n", encoding="utf-8")
            initial = git.metadata_signature(root)

            (gitdir / "index").write_bytes(b"a longer index")
            staged = git.metadata_signature(root)
            branch.write_text("a-longer-commit-id\n", encoding="utf-8")
            committed = git.metadata_signature(root)

        self.assertNotEqual(initial, staged)
        self.assertNotEqual(staged, committed)

    def test_visible_nested_directories_are_scanned_for_repositories(self):
        root = Path("work").resolve()
        top = SimpleNamespace(
            path=root / "source", is_dir=True, is_parent=False, depth=0)
        nested = SimpleNamespace(
            path=root / "source" / "repos" / "project",
            is_dir=True, is_parent=False, depth=2)
        parent = SimpleNamespace(
            path=root.parent, is_dir=True, is_parent=True, depth=0)
        explorer = SimpleNamespace(entries=[parent, top, nested])

        self.assertEqual(NshApp._child_directories(explorer),
                         (top.path, nested.path))

    def test_aggregation_stops_at_repository_root(self):
        root = Path("repo").resolve()
        status = GitStatus(is_repo=True, root=root)

        status.add_file(root / "changed.txt", "M")

        self.assertEqual(status.code_for(root), "M")
        self.assertIsNone(status.code_for(root.parent))


if __name__ == "__main__":
    unittest.main()
