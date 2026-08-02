import unittest
from pathlib import Path

from nsh.explorer.git import GitStatus


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

    def test_aggregation_stops_at_repository_root(self):
        root = Path("repo").resolve()
        status = GitStatus(is_repo=True, root=root)

        status.add_file(root / "changed.txt", "M")

        self.assertEqual(status.code_for(root), "M")
        self.assertIsNone(status.code_for(root.parent))


if __name__ == "__main__":
    unittest.main()
