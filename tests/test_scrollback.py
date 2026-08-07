import unittest
from types import SimpleNamespace

from nsh import config
from nsh.network.shell import RemoteShellView
from nsh.shell.view import ShellView


class ScrollbackTests(unittest.TestCase):
    def test_default_and_preference_validation(self):
        self.assertEqual("2000", config.DEFAULT_SETTINGS["scrollback_lines"])
        self.assertEqual(
            "5000", config.validate_preference(
                "general", "scrollback_lines", "5000"))
        for invalid in ("0", "100001", "many"):
            with self.assertRaises(ValueError):
                config.validate_preference(
                    "general", "scrollback_lines", invalid)

    def test_local_shell_trims_to_live_limit_and_adjusts_scroll(self):
        shell = ShellView.__new__(ShellView)
        shell.app = SimpleNamespace(settings={"scrollback_lines": "3"})
        shell.lines = [[("", str(i))] for i in range(5)]
        shell.scroll_top = 4

        shell.trim_scrollback()

        self.assertEqual(["2", "3", "4"],
                         [line[0][1] for line in shell.lines])
        self.assertEqual(2, shell.scroll_top)

    def test_local_push_never_exceeds_limit(self):
        shell = ShellView.__new__(ShellView)
        shell.app = SimpleNamespace(settings={"scrollback_lines": "2"})
        shell.lines = []
        shell.scroll_top = None

        for value in range(4):
            shell._push([("", str(value))])

        self.assertEqual(["2", "3"], [line[0][1] for line in shell.lines])

    def test_remote_shell_uses_same_limit(self):
        shell = RemoteShellView.__new__(RemoteShellView)
        shell.app = SimpleNamespace(settings={"scrollback_lines": "2"})
        shell.lines = []

        shell.append("one\ntwo\nthree")

        self.assertEqual(["two", "three"],
                         [line[0][1] for line in shell.lines])


if __name__ == "__main__":
    unittest.main()
