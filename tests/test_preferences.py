import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from nsh import config
from nsh.app import EXPLORER, PREFERENCES, NshApp
from nsh.preferences.view import PreferencesView


class PreferenceTests(unittest.TestCase):
    def test_save_preference_preserves_comments_and_other_sections(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nshrc"
            path.write_text(
                "# keep me\n[general]\n# editor = vi\ntwo_pane = false\n\n"
                "[keys]\ncopy = y\n",
                encoding="utf-8",
            )
            with mock.patch("nsh.config.config_path", return_value=path):
                config.save_preference("general", "two_pane", "yes")
                config.save_preference("keys", "copy", "c-y")

            saved = path.read_text(encoding="utf-8")
            self.assertIn("# keep me", saved)
            self.assertIn("# editor = vi", saved)
            self.assertIn("two_pane = true", saved)
            self.assertIn("copy = c-y", saved)

    def test_blank_color_restores_default_and_blank_key_unbinds(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nshrc"
            path.write_text(
                "[colors]\nexplorer.dir = #ffffff\n[keys]\ncopy = c-y\n",
                encoding="utf-8",
            )
            with mock.patch("nsh.config.config_path", return_value=path):
                config.save_preference("colors", "explorer.dir", None)
                config.save_preference("keys", "copy", "")
                colors, keys, _settings, warning = config.load_user_config()

            self.assertIsNone(warning)
            self.assertNotIn("explorer.dir", colors)
            self.assertIn("copy", keys)
            self.assertEqual(keys["copy"], "")
            self.assertIn("copy = none", path.read_text(encoding="utf-8"))

    def test_reset_shortcut_removes_unbind_override(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nshrc"
            path.write_text("[keys]\ncopy = none\n", encoding="utf-8")
            with mock.patch("nsh.config.config_path", return_value=path):
                config.save_preference("keys", "copy", None)
                _colors, keys, _settings, warning = config.load_user_config()

            self.assertIsNone(warning)
            self.assertNotIn("copy", keys)
            self.assertNotIn("copy =", path.read_text(encoding="utf-8"))

    def test_reset_variable_removes_override_and_restores_default(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nshrc"
            path.write_text("[general]\ntwo_pane = true\n", encoding="utf-8")
            with mock.patch("nsh.config.config_path", return_value=path):
                config.save_preference("general", "two_pane", None)
                _colors, _keys, settings, warning = config.load_user_config()

            self.assertIsNone(warning)
            self.assertEqual(settings["two_pane"],
                             config.DEFAULT_SETTINGS["two_pane"])
            self.assertNotIn("two_pane =", path.read_text(encoding="utf-8"))

    def test_invalid_preference_is_rejected_without_writing(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nshrc"
            original = "[general]\nsort = name\n"
            path.write_text(original, encoding="utf-8")
            with mock.patch("nsh.config.config_path", return_value=path):
                with self.assertRaises(ValueError):
                    config.save_preference("general", "sort", "random")
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_open_preferences_switches_to_dedicated_view(self):
        class Session:
            mode = EXPLORER

        class Shells:
            @staticmethod
            def current():
                return Session()

        app = object.__new__(NshApp)
        app.shells = Shells()
        app.switch_mode = mock.Mock()
        with mock.patch("nsh.config.ensure_default_config"):
            app.open_preferences()

        self.assertEqual(app._preferences_return, EXPLORER)
        app.switch_mode.assert_called_once_with(PREFERENCES)

    def test_preferences_view_searches_category_name_and_value(self):
        class App:
            def invalidate(self):
                pass

            def double_click(self, _tag, _index):
                return False

        view = PreferencesView(App())
        with mock.patch("nsh.preferences.view.config.load_user_config",
                        return_value=(
                            {"explorer.dir": "#abcdef"},
                            {"copy": "c-y"},
                            dict(config.DEFAULT_SETTINGS), None)):
            view.refresh()

        categories = {entry.category for entry in view.entries}
        self.assertEqual(categories, {"Variables", "Colors", "Shortcuts"})
        view.query_buffer.text = "colors abcdef"
        self.assertEqual([(entry.category, entry.name) for entry in view.filtered],
                         [("Colors", "explorer.dir")])
        view.query_buffer.text = "shortcuts copy c-y"
        self.assertEqual([(entry.category, entry.name) for entry in view.filtered],
                         [("Shortcuts", "copy")])

    def test_modified_preferences_are_starred_and_offer_reset(self):
        class App:
            def invalidate(self):
                pass

            def double_click(self, _tag, _index):
                return False

        app = App()
        app._edit_preference = mock.Mock()
        view = PreferencesView(app)
        with mock.patch("nsh.preferences.view.config.load_user_config",
                        return_value=(
                            {"explorer.dir": "#abcdef"}, {},
                            dict(config.DEFAULT_SETTINGS), None)):
            view.refresh()

        view.query_buffer.text = "explorer.dir"
        self.assertTrue(view.current().modified)
        self.assertTrue("".join(text for _style, text in view._list_text())
                        .startswith("› * "))
        view.edit()
        self.assertTrue(app._edit_preference.call_args.kwargs["modified"])

    def test_unbound_shortcut_is_shown_as_modified(self):
        class App:
            def invalidate(self):
                pass

            def double_click(self, _tag, _index):
                return False

        view = PreferencesView(App())
        with mock.patch("nsh.preferences.view.config.load_user_config",
                        return_value=(
                            {}, {"copy": ""}, dict(config.DEFAULT_SETTINGS), None)):
            view.refresh()

        entry = next(entry for entry in view.entries if entry.name == "copy")
        self.assertEqual(entry.value, "(unbound)")
        self.assertTrue(entry.modified)
        self.assertTrue(entry.blank_unbinds)

    def test_clear_search_immediately_restores_full_list(self):
        class App:
            def invalidate(self):
                self.invalidated = True

            def double_click(self, _tag, _index):
                return False

        app = App()
        view = PreferencesView(app)
        with mock.patch("nsh.preferences.view.config.load_user_config",
                        return_value=(
                            {}, {}, dict(config.DEFAULT_SETTINGS), None)):
            view.refresh()

        total = len(view.entries)
        view.query_buffer.text = "explorer.dir"
        self.assertLess(len(view.filtered), total)
        view.clear_search()

        self.assertEqual(view.query_buffer.text, "")
        self.assertEqual(len(view.filtered), total)
        self.assertEqual(view.cursor, 0)
        self.assertTrue(app.invalidated)

    def test_all_default_colors_and_shortcuts_are_valid_preferences(self):
        for name, value in config.STYLE_DEFAULTS.items():
            self.assertEqual(config.validate_preference("colors", name, value),
                             value)
        for name, value in config.DEFAULT_KEYS.items():
            expected = "space" if value == " " else value
            self.assertEqual(config.validate_preference("keys", name, expected),
                             expected)
