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


if __name__ == "__main__":
    unittest.main()
