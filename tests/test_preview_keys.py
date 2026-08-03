import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh.explorer.preview import PreviewView
from nsh.explorer.git import GitStatus


class PreviewKeyTests(unittest.TestCase):
    def test_h_returns_focus_to_active_list(self):
        view = object.__new__(PreviewView)
        view.app = SimpleNamespace(
            keys={}, focus_active_list=mock.Mock(), invalidate=mock.Mock())
        bindings = view._kb().get_bindings_for_keys(("h",))

        self.assertEqual(len(bindings), 1)
        bindings[0].handler(SimpleNamespace())

        view.app.focus_active_list.assert_called_once_with()

    def test_tracked_changed_explorer_file_uses_git_preview(self):
        cwd = Path("repo").resolve()
        path = cwd / "src" / "app.py"
        status = GitStatus(is_repo=True, root=cwd)
        status.add_file(path, "M")
        entry = SimpleNamespace(path=path, name="app.py", is_dir=False)
        view = object.__new__(PreviewView)
        view.app = SimpleNamespace(
            cwd=cwd, explorer=SimpleNamespace(git_status=status))

        adapted = view._explorer_git_entry(entry)

        self.assertEqual(adapted.path, path)
        self.assertEqual(adapted.code, "M")
        self.assertEqual(adapted.rel, "src/app.py")

    def test_clean_untracked_and_directory_entries_keep_normal_preview(self):
        cwd = Path("repo").resolve()
        status = GitStatus(is_repo=True, root=cwd)
        untracked = cwd / "draft.txt"
        status.add_file(untracked, "?")
        view = object.__new__(PreviewView)
        view.app = SimpleNamespace(
            cwd=cwd, explorer=SimpleNamespace(git_status=status))

        self.assertIsNone(view._explorer_git_entry(SimpleNamespace(
            path=untracked, name="draft.txt", is_dir=False)))
        self.assertIsNone(view._explorer_git_entry(SimpleNamespace(
            path=cwd / "src", name="src", is_dir=True)))

    def test_changed_file_in_expanded_child_repo_uses_git_preview(self):
        cwd = Path("work").resolve()
        repo = cwd / "project"
        path = repo / "src" / "app.py"
        child = GitStatus(is_repo=True, root=repo)
        child.add_file(path, "M")
        outer = GitStatus(
            child_repos={str(repo).lower(): "RD"},
            child_statuses={str(repo).lower(): child})
        view = object.__new__(PreviewView)
        view.app = SimpleNamespace(
            cwd=cwd, explorer=SimpleNamespace(git_status=outer),
            invalidate=mock.Mock())
        view._cache = {}
        view._inflight = set()
        entry = SimpleNamespace(path=path, name="app.py", is_dir=False)

        adapted = view._explorer_git_entry(entry)

        self.assertEqual(adapted.code, "M")
        self.assertEqual(adapted.rel, "src/app.py")
        self.assertEqual(adapted.git_cwd, repo)
        key = ("test",)
        with mock.patch("nsh.explorer.preview.git.diff",
                        new=mock.AsyncMock(return_value="")) as diff:
            asyncio.run(view._load_git(adapted, key))
        diff.assert_awaited_once_with(path, repo)


if __name__ == "__main__":
    unittest.main()
