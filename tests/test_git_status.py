import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh import config
from nsh.app import NshApp
from nsh.explorer import git
from nsh.explorer.git import GitStatus
from nsh.explorer.view import ExplorerView


class GitStatusTests(unittest.TestCase):
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

        async def output(args, cwd):
            if args[0] == "rev-parse":
                return str(parent if cwd == nested else cwd)
            return " M changed.txt" if cwd == dirty else ""

        with mock.patch("nsh.explorer.git._out", side_effect=output):
            found = asyncio.run(git.child_repositories((clean, dirty, nested)))

        self.assertFalse(found[str(clean).lower()].files)
        self.assertTrue(found[str(dirty).lower()].files)
        self.assertNotIn(str(nested).lower(), found)

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
