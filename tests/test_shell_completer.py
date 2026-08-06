import unittest
from types import SimpleNamespace

from nsh.shell.completer import ShellCompleter


class ShellCompleterTests(unittest.TestCase):
    @staticmethod
    def _completer(is_posix=False):
        runner = SimpleNamespace(_is_posix=is_posix)
        app = SimpleNamespace(shell=SimpleNamespace(runner=runner))
        return ShellCompleter(app)

    def test_powershell_keeps_tilde_in_quoted_file_completion(self):
        completer = self._completer()
        quoted = completer._quote(r"~\Desktop\ab cd", is_dir=False)
        self.assertEqual(quoted, r'"~\Desktop\ab cd"')

    def test_posix_keeps_tilde_outside_quotes(self):
        completer = self._completer(is_posix=True)
        quoted = completer._quote("~/Desktop/ab cd", is_dir=False)
        self.assertEqual(quoted, '~/' + '"Desktop/ab cd"')

    def test_powershell_directory_keeps_tilde_and_quote_open_for_drilling(self):
        completer = self._completer()
        quoted = completer._quote("~\\Desktop\\folder name\\", is_dir=True)
        self.assertEqual(quoted, '"~\\Desktop\\folder name\\')


if __name__ == "__main__":
    unittest.main()
