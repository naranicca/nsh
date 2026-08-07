import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh import config
from nsh.app import NshApp
from nsh.shell.tabs import ShellTabs, restored_tab_specs


class TabRestoreTests(unittest.TestCase):
    def test_restore_tabs_is_enabled_by_default_and_validates_as_boolean(self):
        self.assertEqual("true", config.DEFAULT_SETTINGS["restore_tabs"])
        self.assertEqual(
            "false", config.validate_preference("general", "restore_tabs", "off"))

    def test_restored_specs_keep_paths_layout_title_and_active_tab(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            snapshot = {
                "active": 1,
                "tabs": [
                    {"paths": [str(first), str(first)]},
                    {"paths": [str(second), str(first)], "active_pane": 1,
                     "two_pane": True, "title": "work"},
                ],
            }

            specs, active = restored_tab_specs(snapshot)

        self.assertEqual(2, len(specs))
        self.assertEqual(1, active)
        self.assertEqual([second, first], specs[1]["paths"])
        self.assertEqual(1, specs[1]["active_pane"])
        self.assertTrue(specs[1]["two_pane"])
        self.assertEqual("work", specs[1]["title"])

    def test_missing_tabs_are_skipped_and_active_index_is_remapped(self):
        with tempfile.TemporaryDirectory() as root:
            valid = Path(root)
            snapshot = {
                "active": 2,
                "tabs": [
                    {"paths": [str(valid / "missing-before")]},
                    {"paths": [str(valid)]},
                    {"paths": [str(valid)]},
                    {"paths": [str(valid / "missing-after")]},
                ],
            }

            specs, active = restored_tab_specs(snapshot)

        self.assertEqual(2, len(specs))
        self.assertEqual(1, active)

    def test_snapshot_serializes_both_pane_paths(self):
        tabs = ShellTabs.__new__(ShellTabs)
        tabs.active = 0
        tabs.sessions = [SimpleNamespace(
            explorers=[SimpleNamespace(cwd=Path("left")),
                       SimpleNamespace(cwd=Path("right"))],
            active_pane=1, two_pane=True, custom_title="pair")]

        snapshot = tabs.snapshot()

        self.assertEqual(["left", "right"], snapshot["tabs"][0]["paths"])
        self.assertEqual(1, snapshot["tabs"][0]["active_pane"])
        self.assertTrue(snapshot["tabs"][0]["two_pane"])
        self.assertEqual("pair", snapshot["tabs"][0]["title"])

    def test_save_state_respects_setting_and_skips_search_picker(self):
        app = NshApp.__new__(NshApp)
        app.picker = False
        app._restore_tabs = True
        snapshot = {"version": 1, "tabs": []}
        app.shells = SimpleNamespace(snapshot=lambda: snapshot)
        with mock.patch("nsh.app.state.set") as save:
            app._save_tab_state()
            save.assert_called_once_with("explorer_tabs", snapshot)

            save.reset_mock()
            app._restore_tabs = False
            app._save_tab_state()
            save.assert_called_once_with("explorer_tabs", None)

            save.reset_mock()
            app.picker = True
            app._save_tab_state()
            save.assert_not_called()

    def test_restored_tab_is_loaded_only_once_when_selected(self):
        loads = []
        session = SimpleNamespace(
            _needs_initial_load=True,
            explorers=[SimpleNamespace(load=lambda: loads.append("left")),
                       SimpleNamespace(load=lambda: loads.append("right"))])
        tabs = ShellTabs.__new__(ShellTabs)

        tabs.ensure_loaded(session)
        tabs.ensure_loaded(session)

        self.assertEqual(["left", "right"], loads)
        self.assertFalse(session._needs_initial_load)


if __name__ == "__main__":
    unittest.main()
