import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh import config
from nsh.explorer import fileops
from nsh.explorer.view import ExplorerView


class TrashFileOpsTests(unittest.TestCase):
    def test_macos_trash_uses_unique_name(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "document.txt"
            source.write_text("new", encoding="utf-8")
            trash_dir = root / ".Trash"
            trash_dir.mkdir()
            (trash_dir / source.name).write_text("old", encoding="utf-8")

            with mock.patch.object(Path, "home", return_value=root):
                fileops._trash_macos(source)

            self.assertFalse(source.exists())
            self.assertEqual("new", (trash_dir / "document (2).txt").read_text(
                encoding="utf-8"))

    def test_freedesktop_trash_writes_restore_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "file with spaces.txt"
            source.write_text("data", encoding="utf-8")
            data_home = root / "data"

            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data_home)}):
                fileops._trash_freedesktop(source)

            moved = data_home / "Trash/files" / source.name
            info = data_home / "Trash/info" / f"{source.name}.trashinfo"
            self.assertTrue(moved.exists())
            metadata = info.read_text(encoding="utf-8")
            self.assertIn("[Trash Info]", metadata)
            self.assertIn("file%20with%20spaces.txt", metadata)
            self.assertIn("DeletionDate=", metadata)

    def test_freedesktop_trash_does_not_overwrite_orphan_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "note.txt"
            source.write_text("new", encoding="utf-8")
            data_home = root / "data"
            info_dir = data_home / "Trash/info"
            info_dir.mkdir(parents=True)
            (info_dir / "note.txt.trashinfo").write_text(
                "old metadata", encoding="utf-8")

            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data_home)}):
                fileops._trash_freedesktop(source)

            self.assertEqual("old metadata", (info_dir / "note.txt.trashinfo").read_text(
                encoding="utf-8"))
            self.assertTrue((data_home / "Trash/files/note (2).txt").exists())

    def test_platform_dispatch_uses_windows_recycle_bin(self):
        path = Path("item.txt")
        with (mock.patch("nsh.explorer.fileops.sys.platform", "win32"),
              mock.patch("nsh.explorer.fileops._trash_windows") as recycle):
            asyncio.run(fileops.trash(path))
        recycle.assert_called_once_with(path)

    def test_lowercase_d_is_trash_and_uppercase_d_is_permanent_delete(self):
        self.assertEqual("d", config.DEFAULT_KEYS["trash"])
        self.assertEqual("D", config.DEFAULT_KEYS["delete"])

    def test_trash_entry_confirms_before_moving(self):
        target = Path("selected.txt")
        app = SimpleNamespace()
        confirmations = []
        app.confirm = lambda label, callback: confirmations.append((label, callback))
        view = ExplorerView.__new__(ExplorerView)
        view.app = app
        view._targets = lambda: [target]

        view.trash_entry()

        self.assertEqual("Move 'selected.txt' to the Trash?", confirmations[0][0])


if __name__ == "__main__":
    unittest.main()
