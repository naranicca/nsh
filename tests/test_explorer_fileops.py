import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh.explorer import fileops
from nsh.explorer.view import ExplorerView


class ExplorerFileOperationTests(unittest.TestCase):
    def test_delete_removes_tree_with_read_only_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tree"
            root.mkdir()
            child = root / "read-only.txt"
            child.write_text("data", encoding="utf-8")
            child.chmod(stat.S_IREAD)

            asyncio.run(fileops.delete(root))

            self.assertFalse(root.exists())

    def test_delete_reports_failures_in_error_dialog(self):
        errors = []
        app = SimpleNamespace(
            show_error=lambda title, lines: errors.append((title, lines)),
            set_message=mock.Mock(), refresh_git=mock.AsyncMock())
        view = ExplorerView.__new__(ExplorerView)
        view.app = app
        view.selected = set()
        view.refresh_listing = mock.Mock()

        async def scenario():
            with mock.patch("nsh.explorer.view.fileops.delete",
                            mock.AsyncMock(side_effect=PermissionError("denied"))):
                view._do_delete([Path("locked")], True)
                await asyncio.sleep(0)

        asyncio.run(scenario())

        self.assertEqual([("Delete failed", ["locked: denied"])], errors)

    def test_copy_can_overwrite_a_confirmed_existing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            target_dir = root / "target"
            source_dir.mkdir()
            target_dir.mkdir()
            source = source_dir / "report.txt"
            source.write_text("new", encoding="utf-8")
            (target_dir / "report.txt").write_text("old", encoding="utf-8")

            copied = asyncio.run(fileops.copy(
                source, target_dir, name="report.txt", overwrite=True))

            self.assertEqual(target_dir / "report.txt", copied)
            self.assertEqual("new", copied.read_text(encoding="utf-8"))

    def test_explicit_existing_target_requires_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            target_dir = root / "target"
            source_dir.mkdir()
            target_dir.mkdir()
            source = source_dir / "report.txt"
            source.write_text("new", encoding="utf-8")
            (target_dir / "report.txt").write_text("old", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                asyncio.run(fileops.copy(source, target_dir, name="report.txt"))


if __name__ == "__main__":
    unittest.main()
