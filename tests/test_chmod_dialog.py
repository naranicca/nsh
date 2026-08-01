import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh.explorer.view import ExplorerView
from nsh.util.dialog import ChmodDialog


class ChmodDialogTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
