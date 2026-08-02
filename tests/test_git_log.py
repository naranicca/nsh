import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh.explorer.logview import LogView
from nsh.explorer.gitview import GitEntry, GitView
from nsh.explorer.view import _git_action_label, _git_log_label


class GitLogTests(unittest.TestCase):
    def test_action_label_identifies_file_filter(self):
        self.assertEqual(_git_log_label(()), "Git: Log .")
        self.assertEqual(_git_log_label((Path("notes.txt"),)),
                         "Git: Log notes.txt")
        self.assertEqual(_git_log_label((Path("a"), Path("b"), Path("c"))),
                         "Git: Log 3 files")
        self.assertEqual(_git_action_label("Git: Revert", ()),
                         "Git: Revert .")

    def test_git_mode_log_uses_selected_files_and_label(self):
        paths = (Path("repo/one.txt"), Path("repo/two.txt"))
        status = SimpleNamespace(
            dirty=True, can_pull=False, can_push=False, in_progress=None,
            has_stash=False, has_commits=True,
        )
        app = SimpleNamespace(
            cwd=Path("repo"),
            git_status=status,
            explorer=SimpleNamespace(
                git_branches=mock.Mock(), git_commit=mock.Mock(),
                git_commit_all=mock.Mock(),
                git_stash=mock.Mock()),
            open_log=mock.Mock(), open_menu=mock.Mock(),
        )
        view = object.__new__(GitView)
        view.app = app
        view.entries = [GitEntry(path, "M", path.name) for path in paths]
        view.cursor = 0
        view.selected = set(paths)

        view.open_action_menu()

        items = app.open_menu.call_args.args[1]
        labels = [item[0] for item in items]
        self.assertIn("Git: Log .", labels)
        self.assertIn("Git: Revert .", labels)
        self.assertIn("Git: Revert 2 files", labels)
        self.assertLess(labels.index("Git: Revert ."),
                        labels.index("Git: Revert 2 files"))
        self.assertIn("Git: Commit .", labels)
        self.assertIn("Git: Commit 2 files", labels)
        self.assertLess(labels.index("Git: Commit ."),
                        labels.index("Git: Commit 2 files"))
        label, action = next(item for item in items
                             if item[0] == "Git: Log 2 files")
        self.assertEqual(label, "Git: Log 2 files")
        action()
        app.open_log.assert_called_once_with(paths)

    def test_selected_file_log_passes_relative_pathspec(self):
        cwd = Path("repo") / "subdir"
        app = SimpleNamespace(cwd=cwd, keys={}, invalidate=mock.Mock(),
                              close_log=mock.Mock())
        view = LogView(app)
        view.path_filters = (cwd / "file name.txt",)

        async def load():
            with mock.patch("nsh.explorer.logview.git.log_graph",
                            new=mock.AsyncMock(return_value="")) as log_graph:
                await view._load()
                log_graph.assert_awaited_once_with(cwd, ("file name.txt",))

        asyncio.run(load())

    def test_repository_log_has_no_pathspec(self):
        cwd = Path("repo")
        app = SimpleNamespace(cwd=cwd, keys={}, invalidate=mock.Mock(),
                              close_log=mock.Mock())
        view = LogView(app)

        async def load():
            with mock.patch("nsh.explorer.logview.git.log_graph",
                            new=mock.AsyncMock(return_value="")) as log_graph:
                await view._load()
                log_graph.assert_awaited_once_with(cwd, ())

        asyncio.run(load())

    def test_multiple_selected_files_are_all_passed_as_pathspecs(self):
        cwd = Path("repo")
        app = SimpleNamespace(cwd=cwd, keys={}, invalidate=mock.Mock(),
                              close_log=mock.Mock())
        view = LogView(app)
        view.path_filters = (cwd / "one.txt", cwd / "two.txt")

        async def load():
            with mock.patch("nsh.explorer.logview.git.log_graph",
                            new=mock.AsyncMock(return_value="")) as log_graph:
                await view._load()
                log_graph.assert_awaited_once_with(cwd, ("one.txt", "two.txt"))

        asyncio.run(load())


if __name__ == "__main__":
    unittest.main()
