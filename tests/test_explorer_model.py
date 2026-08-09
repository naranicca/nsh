import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh.explorer.model import Entry, list_dir
from nsh.explorer.view import ExplorerView


class _Scan(list):
    def __enter__(self):
        return iter(self)

    def __exit__(self, exc_type, exc, traceback):
        return False


class ExplorerModelTests(unittest.TestCase):
    def test_list_dir_records_raw_symbolic_link_target(self):
        directory_entry = SimpleNamespace(
            name="current",
            path="/work/current",
            is_symlink=lambda: True,
            is_dir=lambda: True,
            stat=lambda follow_symlinks=False: SimpleNamespace(
                st_mtime_ns=1, st_size=0),
        )
        with mock.patch("nsh.explorer.model.os.scandir",
                        return_value=_Scan([directory_entry])), mock.patch(
                "nsh.explorer.model.os.readlink",
                return_value="../releases/v2"):
            entries = list_dir(Path("/work"))

        self.assertEqual(entries[0].link_target, "../releases/v2")

    def test_symbolic_link_display_matches_remote_arrow_format(self):
        directory = Entry(
            Path("current"), "current", True, True, False, False, 0,
            link_target="../releases/v2")
        file_link = Entry(
            Path("settings"), "settings", False, True, False, False, 0,
            link_target="config/settings.json")

        self.assertEqual(
            ExplorerView._display_name(directory),
            "current" + os.sep + " -> ../releases/v2" + os.sep,
        )
        self.assertEqual(
            ExplorerView._display_name(file_link),
            "settings -> config/settings.json",
        )

    def test_directory_link_target_has_exactly_one_trailing_separator(self):
        for target in ("../releases/v2/", "../releases/v2\\"):
            entry = Entry(
                Path("current"), "current", True, True, False, False, 0,
                link_target=target)
            self.assertEqual(
                "current" + os.sep + " -> ../releases/v2" + os.sep,
                ExplorerView._display_name(entry),
            )


if __name__ == "__main__":
    unittest.main()
