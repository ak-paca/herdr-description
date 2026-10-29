import json
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from herdr_description.config import (
    Config,
    ConfigError,
    DEFAULT_HOTKEYS,
    keybindings,
    load_config,
    parse_config,
    toggle_autosave,
    write_config,
)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "configuration"

    def put(self, values):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "config.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        return path

    def test_missing_configuration_creates_private_defaults(self):
        config = load_config(self.directory)
        self.assertEqual(Config(), config)
        path = self.directory / "config.json"
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(config, load_config(self.directory))
        self.assertTrue(path.read_bytes().endswith(b"\n"))
        self.assertEqual([path], list(self.directory.iterdir()))
        self.assertNotIn("editor", json.loads(path.read_text(encoding="utf-8")))
        self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["autosave_enabled"])
        self.assertNotIn("autosave_interval_ms", json.loads(path.read_text(encoding="utf-8")))

    def test_defaults_do_not_share_mutable_values(self):
        first = Config()
        first.hotkeys["edit-pane"] = ""
        self.assertEqual(DEFAULT_HOTKEYS["edit-pane"], Config().hotkeys["edit-pane"])
        self.assertFalse(hasattr(Config(), "editor"))

    def test_handwritten_updates_reload_and_merge_defaults(self):
        load_config(self.directory)
        self.put(
            {
                "panel_width_percent": 60,
                "hotkeys": {"edit-pane": "ctrl+alt+w", "settings": "prefix+alt+c"},
            }
        )
        config = load_config(self.directory)
        self.assertEqual(60, config.panel_width_percent)
        self.assertEqual("ctrl+alt+w", config.hotkeys["edit-pane"])
        self.assertEqual(DEFAULT_HOTKEYS["edit-tab"], config.hotkeys["edit-tab"])

    def test_legacy_save_hotkey_is_ignored_and_removed_on_config_save(self):
        path = self.put({"hotkeys": {"save": "prefix+alt+w", "edit-pane": "prefix+alt+w"}})
        original = path.read_bytes()
        config = load_config(self.directory)
        self.assertNotIn("save", config.hotkeys)
        self.assertEqual("prefix+alt+w", config.hotkeys["edit-pane"])
        self.assertEqual(original, path.read_bytes())
        output = keybindings(config)
        self.assertNotIn("herdr-description.save", output)
        self.assertIn('command = "herdr-description.edit-pane"', output)
        write_config(self.directory, config)
        self.assertNotIn("save", json.loads(path.read_text(encoding="utf-8"))["hotkeys"])
        self.assertEqual(config, load_config(self.directory))

    def test_autosave_setting_is_honored_and_obsolete_interval_is_ignored(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                path = self.put(
                    {
                        "editor": ["nvim"],
                        "autosave_enabled": enabled,
                        "autosave_interval_ms": 250,
                    }
                )
                original = path.read_bytes()
                config = load_config(self.directory)
                self.assertEqual(Config(autosave_enabled=enabled), config)
                self.assertFalse(hasattr(config, "editor"))
                self.assertIs(config.autosave_enabled, enabled)
                self.assertFalse(hasattr(config, "autosave_interval_ms"))
                self.assertEqual(original, path.read_bytes())
                write_config(self.directory, config)
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("editor", saved)
                self.assertIs(saved["autosave_enabled"], enabled)
                self.assertNotIn("autosave_interval_ms", saved)

    def test_invalid_existing_config_is_not_overwritten(self):
        self.directory.mkdir()
        path = self.directory / "config.json"
        for content in ("{ invalid", "[]", '{"hotkeys": {"edit-pane": 42}}', '{"panel_width_percent": 0}'):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, "Invalid configuration"):
                    load_config(self.directory)
                self.assertEqual(content, path.read_text(encoding="utf-8"))
                self.assertEqual([path], list(self.directory.iterdir()))

    def test_existing_configuration_gains_pane_hotkeys_without_rewriting_file(self):
        previous_hotkeys = {
            action: chord for action, chord in DEFAULT_HOTKEYS.items() if action not in {"edit-pane", "remove-pane"}
        }
        previous_hotkeys["edit-tab"] = "ctrl+alt+t"
        path = self.put({"hotkeys": previous_hotkeys})
        original = path.read_bytes()
        config = load_config(self.directory)
        self.assertEqual("ctrl+alt+t", config.hotkeys["edit-tab"])
        self.assertEqual("prefix+alt+p", config.hotkeys["edit-pane"])
        self.assertEqual("prefix+alt+shift+p", config.hotkeys["remove-pane"])
        self.assertEqual(original, path.read_bytes())

    def test_legacy_editor_settings_are_ignored_without_rewriting_file(self):
        for editor in (["vim"], ["/opt/bin/nvim", "-u", "NONE"], ["emacs"], None, "vim", []):
            with self.subTest(editor=editor):
                path = self.put({"editor": editor})
                original = path.read_bytes()
                self.assertEqual(Config(), load_config(self.directory))
                self.assertEqual(original, path.read_bytes())

    def test_close_actions_are_unbound_without_changing_existing_close_shortcuts(self):
        path = self.put({"hotkeys": {"close-tab": "prefix+x", "close-space": "prefix+shift+x"}})
        original = path.read_bytes()
        config = load_config(self.directory)
        self.assertEqual("", config.hotkeys["close-pane"])
        self.assertEqual("prefix+x", config.hotkeys["close-tab"])
        self.assertEqual("prefix+shift+x", config.hotkeys["close-space"])
        self.assertEqual(original, path.read_bytes())
        self.assertTrue(all(Config().hotkeys[action] == "" for action in ("close-pane", "close-tab", "close-space")))

    def test_close_pane_hotkey_targets_confirmation_action(self):
        config = Config()
        config.hotkeys["close-pane"] = "prefix+alt+x"
        output = keybindings(config)
        self.assertIn('command = "herdr-description.close-pane"', output)
        self.assertIn('description = "Close pane with description confirmation"', output)

    def test_invalid_values(self):
        values = [
            {"panel_width_percent": 14},
            {"panel_width_percent": 61},
            {"autosave": True},
            {"autosave_enabled": 0},
            {"autosave_enabled": 1},
            {"autosave_enabled": "false"},
            {"autosave_enabled": None},
            {"autosave_enabled": []},
            {"hotkeys": []},
            {"hotkeys": {"unknown": ""}},
            {"hotkeys": {"edit-pane": None}},
            {"hotkeys": {"edit-pane": " prefix+w"}},
            {"hotkeys": {"edit-pane": "prefix+\n"}},
            {"hotkeys": {"edit-pane": "prefix+alt+t"}},
            {"hotkeys": {"edit-pane": "PREFIX+ALT+T"}},
            {"hotkeys": {"edit-pane": "alt+prefix+t"}},
        ]
        for value in values:
            with self.subTest(value=value):
                self.put(value)
                with self.assertRaises(ConfigError):
                    load_config(self.directory)

    def test_duplicate_json_keys_are_rejected(self):
        self.directory.mkdir()
        path = self.directory / "config.json"
        path.write_text('{"hotkeys": {"save": "prefix+a", "save": "prefix+b"}}', encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "Duplicate JSON key: save"):
            load_config(self.directory)

    def test_parse_settings_before_writing_accepts_defaults_and_legacy_fields(self):
        self.assertEqual(Config(), parse_config("{}"))
        config = parse_config(
            '{"editor": ["nvim"], "autosave_enabled": false, "autosave_interval_ms": 250, '
            '"panel_width_percent": 40, "hotkeys": {"close-pane": "prefix+alt+x"}}'
        )
        self.assertEqual(40, config.panel_width_percent)
        self.assertFalse(config.autosave_enabled)
        self.assertEqual("prefix+alt+x", config.hotkeys["close-pane"])
        self.assertFalse(hasattr(config, "editor"))
        self.assertFalse(self.directory.exists())

    def test_parse_settings_rejects_invalid_content_before_writing(self):
        for content in (
            "{invalid",
            "[]",
            '"text"',
            "null",
            '{"hotkeys": false}',
            '{"panel_width_percent": true}',
            '{"unknown": 1}',
            '{"hotkeys": {"save": "prefix+a", "save": "prefix+b"}}',
            '{"hotkeys": {}, "hotkeys": {}}',
        ):
            with self.subTest(content=content):
                with self.assertRaises(ConfigError):
                    parse_config(content)
        self.assertFalse(self.directory.exists())

    def test_write_round_trip_and_unbound_actions(self):
        config = Config(panel_width_percent=15)
        config.hotkeys = dict.fromkeys(DEFAULT_HOTKEYS, "")
        path = write_config(self.directory, config)
        self.assertEqual(self.directory / "config.json", path)
        self.assertEqual(config, load_config(self.directory))
        self.assertNotIn("[[keys.command]]", keybindings(config))

    def test_invalid_write_preserves_existing_file(self):
        load_config(self.directory)
        path = self.directory / "config.json"
        original = path.read_bytes()
        with self.assertRaises(ConfigError):
            write_config(self.directory, Config(panel_width_percent=200))
        self.assertEqual(original, path.read_bytes())

    def test_toggle_autosave_persists_and_preserves_other_settings(self):
        original = Config(panel_width_percent=45)
        original.hotkeys["edit-pane"] = "ctrl+alt+w"
        write_config(self.directory, original)
        disabled = toggle_autosave(self.directory)
        self.assertFalse(disabled.autosave_enabled)
        self.assertEqual(disabled, load_config(self.directory))
        self.assertEqual(disabled.hotkeys, original.hotkeys)
        self.assertEqual(disabled.panel_width_percent, original.panel_width_percent)
        self.assertEqual(toggle_autosave(self.directory), original)

    def test_invalid_autosave_values_cannot_be_loaded_toggled_or_written(self):
        for value in (0, 1, "true", "false", None, [], {}):
            with self.subTest(value=value):
                path = self.put({"autosave_enabled": value, "panel_width_percent": 45})
                original = path.read_bytes()
                with self.assertRaisesRegex(ConfigError, "autosave_enabled must be a boolean"):
                    load_config(self.directory)
                with self.assertRaisesRegex(ConfigError, "autosave_enabled must be a boolean"):
                    toggle_autosave(self.directory)
                with self.assertRaisesRegex(ConfigError, "autosave_enabled must be a boolean"):
                    write_config(self.directory, Config(autosave_enabled=value))
                self.assertEqual(path.read_bytes(), original)

    def test_concurrent_toggles_do_not_lose_updates(self):
        original = Config(panel_width_percent=45)
        original.hotkeys["edit-pane"] = "ctrl+alt+w"
        write_config(self.directory, original)
        barrier = Barrier(8)

        def toggle_repeatedly(_):
            barrier.wait(timeout=5)
            return [toggle_autosave(self.directory).autosave_enabled for _ in range(5)]

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(toggle_repeatedly, range(8)))
        values = [value for result in results for value in result]
        self.assertEqual(values.count(True), 20)
        self.assertEqual(values.count(False), 20)
        self.assertEqual(load_config(self.directory), original)

    def test_failed_replace_preserves_existing_file_and_removes_temporary(self):
        load_config(self.directory)
        path = self.directory / "config.json"
        original = path.read_bytes()
        with patch("herdr_description.config.os.replace", side_effect=OSError("failure")):
            with self.assertRaises(OSError):
                write_config(self.directory, Config(panel_width_percent=40))
        self.assertEqual(original, path.read_bytes())
        self.assertEqual([path], list(self.directory.iterdir()))

    def test_create_defaults_preserves_concurrently_created_configuration(self):
        real_link = __import__("os").link

        def concurrent_link(source, destination):
            Path(destination).write_text('{"panel_width_percent": 40}', encoding="utf-8")
            return real_link(source, destination)

        with patch("herdr_description.config.os.link", side_effect=concurrent_link):
            config = load_config(self.directory)
        self.assertEqual(40, config.panel_width_percent)
        self.assertEqual(1, len(list(self.directory.iterdir())))

    def test_keybindings_are_marked_and_target_plugin_actions(self):
        output = keybindings(Config())
        self.assertTrue(output.startswith("# BEGIN herdr-description keybindings\n"))
        self.assertTrue(output.endswith("# END herdr-description keybindings\n"))
        self.assertEqual(7, output.count("[[keys.command]]"))
        self.assertEqual(7, output.count('type = "plugin_action"'))
        self.assertIn('command = "herdr-description.toggle-indicators"', output)
        self.assertIn('key = "prefix+alt+i"', output)
        self.assertIn('command = "herdr-description.edit-tab"', output)
        self.assertIn('description = "Create/edit tab description"', output)
        self.assertIn('command = "herdr-description.edit-pane"', output)
        self.assertIn('description = "Create/edit pane description"', output)
        self.assertIn('command = "herdr-description.remove-pane"', output)
        self.assertIn('description = "Remove pane description"', output)
        self.assertNotIn("herdr-description.settings", output)
        self.assertNotIn("herdr-description.save", output)

    def test_keybindings_escape_toml_strings(self):
        config = Config()
        config.hotkeys["edit-pane"] = 'prefix+alt+"'
        config.hotkeys["settings"] = "prefix+alt+\\"
        output = keybindings(config)
        self.assertIn('key = "prefix+alt+\\""', output)
        self.assertIn('key = "prefix+alt+\\\\"', output)
        try:
            import tomllib
        except ImportError:
            return
        parsed = tomllib.loads(output)
        chords = {item["command"]: item["key"] for item in parsed["keys"]["command"]}
        self.assertEqual(config.hotkeys["edit-pane"], chords["herdr-description.edit-pane"])
        self.assertEqual(config.hotkeys["settings"], chords["herdr-description.settings"])


if __name__ == "__main__":
    unittest.main()
