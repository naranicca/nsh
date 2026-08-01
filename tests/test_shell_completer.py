import os
import unittest
from types import SimpleNamespace

from nsh.shell.completer import ShellCompleter


class ShellCompleterTests(unittest.TestCase):
    @staticmethod
    def _completer(is_posix=False):
        runner = SimpleNamespace(_is_posix=is_posix)
        app = SimpleNamespace(shell=SimpleNamespace(runner=runner))
        return ShellCompleter(app)

    def test_windows_shell_expands_home_before_quoting(self):
        completer = self._completer()
        quoted = completer._quote(r"~\Desktop\ab cd", is_dir=False)
        expected = os.path.expanduser(r"~\Desktop\ab cd")
        self.assertEqual(quoted, '"' + expected + '"')
        self.assertNotIn("~", quoted)

    def test_posix_keeps_tilde_outside_quotes(self):
        completer = self._completer(is_posix=True)
        quoted = completer._quote("~/Desktop/ab cd", is_dir=False)
        self.assertEqual(quoted, '~/' + '"Desktop/ab cd"')

    def test_powershell_directory_keeps_whole_quote_open_for_drilling(self):
        completer = self._completer()
        quoted = completer._quote("~\\Desktop\\folder name\\", is_dir=True)
        expected = os.path.expanduser("~\\Desktop\\folder name\\")
        self.assertEqual(quoted, '"' + expected)


if __name__ == "__main__":
    unittest.main()
