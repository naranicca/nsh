import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh.app import NshApp
from nsh.explorer.view import ExplorerView
from nsh.util.dialog import ChmodDialog, ProgressDialog
from nsh.util.menu import SEPARATOR


class ChmodDialogTests(unittest.TestCase):
    def test_progress_dialog_reports_progress_and_cancel(self):
        cancelled = mock.Mock()
        dialog = ProgressDialog(lambda: None)
        dialog.open("Download", "photo.jpg", cancelled)
        dialog.update(50, 100)

        rendered = "".join(text for fragment in dialog._text()
                           for text in [fragment[1]])
        self.assertIn("50.0%", rendered)
        self.assertIn("photo.jpg", rendered)
        dialog.cancel()
        cancelled.assert_called_once_with()
        self.assertTrue(dialog.active)

    def test_new_items_are_removed_from_f10_menu(self):
        app = object.__new__(NshApp)
        app.open_menu = mock.Mock()

        app.open_nsh_menu()

        labels = [label for label, _callback in app.open_menu.call_args.args[1]]
        self.assertNotIn("New folder", labels)
        self.assertNotIn("New file", labels)

    def test_mode_text_accepts_octal_symbolic_and_chmod_expression(self):
        dialog = ChmodDialog(lambda: None)
        dialog.open("Permissions", 0o644, lambda mode: None)

        self.assertEqual(dialog._parse_mode_text("755"), 0o755)
        self.assertEqual(dialog._parse_mode_text("rwxr-xr-x"), 0o755)
        self.assertEqual(dialog._parse_mode_text("u+x"), 0o744)
        self.assertEqual(dialog._parse_mode_text("go-w"), 0o644)
        self.assertEqual(dialog._parse_mode_text("a=rw"), 0o666)
        self.assertIsNone(dialog._parse_mode_text("999"))

    def test_text_and_checkbox_modes_stay_synchronized(self):
        dialog = ChmodDialog(lambda: None)
        dialog.open("Permissions", 0o644, lambda mode: None)
        for char in "755":
            dialog._edit_text(char)

        self.assertEqual(dialog.mode_text, "755")
        self.assertEqual(dialog._mode(), 0o755)
        dialog._toggle(1)  # owner read off
        self.assertEqual(dialog.mode_text, "355")
        self.assertEqual(dialog._mode(), 0o355)

    def test_invalid_text_does_not_close_or_apply(self):
        applied = []
        dialog = ChmodDialog(lambda: None)
        dialog.open("Permissions", 0o644, applied.append)
        dialog.mode_text = "invalid"

        dialog._accept()

        self.assertTrue(dialog.active)
        self.assertEqual(applied, [])
        self.assertTrue(dialog.error)

    def test_chmod_is_in_tab_menu_on_windows_too(self):
        entry = SimpleNamespace(
            path=Path("sample"), name="sample", is_dir=True, is_image=False)

        class App:
            git_status = None

            def invalidate(self):
                pass

            def open_menu(self, title, items, **kwargs):
                self.opened = (title, items, kwargs)

        view = object.__new__(ExplorerView)
        view.app = App()
        view.selected = set()
        view.clipboard = None
        view.current = lambda: entry

        with mock.patch("nsh.explorer.view.os.name", "nt"):
            view.open_command_menu()

        labels = [label for label, _callback in view.app.opened[1]]
        self.assertIn("chmod…", labels)
        chmod = labels.index("chmod…")
        self.assertEqual(labels[chmod + 1:chmod + 5],
                         [SEPARATOR, "New folder", "New file", SEPARATOR])


if __name__ == "__main__":
    unittest.main()
