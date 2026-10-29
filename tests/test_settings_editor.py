import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from herdr_description.app import edit_settings
from herdr_description.config import load_config, toggle_autosave, write_config


class SettingsEditorTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        load_config(self.directory)
        self.path = self.directory / "config.json"

    def invoke(self, interaction):
        with patch("herdr_description.app.run_editor", side_effect=interaction):
            edit_settings(self.directory)

    def save_payload(self, original, proposed, force=False):
        return {"save": {
            "content": proposed,
            "expected_digest": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "force": force,
        }}

    def test_valid_save_uses_simple_editor_and_returns_normalized_configuration(self):
        def interaction(path, poll):
            self.assertEqual(path, self.path)
            original = poll({})["record"]["content"]
            proposed = json.dumps({"panel_width_percent": 40})
            response = poll(self.save_payload(original, proposed))
            self.assertTrue(response["saved"])
            self.assertEqual(response["record"]["content"], self.path.read_text())
            self.assertEqual(load_config(self.directory).panel_width_percent, 40)
        self.invoke(interaction)

    def test_invalid_configuration_never_replaces_existing_file(self):
        original = self.path.read_text()
        for proposed in (
            "{", '{"panel_width_percent": 1}', '{"hotkeys": {}, "hotkeys": {}}',
            '{"autosave_enabled": "false"}', '{"autosave_enabled": 0}',
        ):
            with self.subTest(proposed=proposed):
                def interaction(path, poll):
                    self.assertIn("error", poll(self.save_payload(original, proposed)))
                    self.assertEqual(path.read_text(), original)
                self.invoke(interaction)

    def test_external_change_requires_explicit_overwrite(self):
        original = self.path.read_text()
        external = '{"panel_width_percent": 35}\n'
        self.path.write_text(external)
        def interaction(path, poll):
            response = poll(self.save_payload(original, '{"panel_width_percent": 45}'))
            self.assertTrue(response["conflict"])
            self.assertEqual(path.read_text(), external)
            response = poll(self.save_payload(original, '{"panel_width_percent": 45}', force=True))
            self.assertTrue(response["saved"])
            self.assertEqual(load_config(self.directory).panel_width_percent, 45)
        self.invoke(interaction)

    def test_crlf_digest_matches_the_editor_file_bytes(self):
        original = '{\r\n  "panel_width_percent": 30\r\n}\r\n'
        self.path.write_bytes(original.encode("utf-8"))
        def interaction(path, poll):
            self.assertEqual(poll({})["record"]["content"], original)
            self.assertTrue(poll(self.save_payload(original, '{"panel_width_percent": 40}'))["saved"])
        self.invoke(interaction)

    def test_settings_editor_does_not_offer_autosave_or_toggle_global_mode(self):
        original = self.path.read_bytes()

        def interaction(path, poll):
            for payload in ({}, {"toggle_autosave": True}, {"modified": True, "content": "draft"}):
                response = poll(payload)
                self.assertFalse(response.get("autosave_supported", False))
                self.assertFalse(response.get("autosave_enabled", False))
                self.assertFalse(response["saved"])
                self.assertEqual(path.read_bytes(), original)
                self.assertTrue(load_config(self.directory).autosave_enabled)

        self.invoke(interaction)

    def test_autosave_toggle_conflicts_with_an_open_settings_draft(self):
        def interaction(path, poll):
            original = poll({})["record"]["content"]
            proposed = json.loads(original)
            proposed["panel_width_percent"] = 45
            toggle_autosave(self.directory)
            toggled = path.read_bytes()
            response = poll(self.save_payload(original, json.dumps(proposed)))
            self.assertTrue(response["conflict"])
            self.assertEqual(path.read_bytes(), toggled)
            self.assertFalse(load_config(self.directory).autosave_enabled)
            response = poll(self.save_payload(original, json.dumps(proposed), force=True))
            self.assertTrue(response["saved"])
            self.assertTrue(load_config(self.directory).autosave_enabled)
            self.assertEqual(load_config(self.directory).panel_width_percent, 45)

        self.invoke(interaction)

    def test_settings_save_and_concurrent_toggle_preserve_both_changes(self):
        callbacks = []
        self.invoke(lambda path, poll: callbacks.append(poll))
        poll = callbacks[0]
        original = poll({})["record"]["content"]
        write_entered = Event()
        release_write = Event()
        toggle_started = Event()
        toggle_loaded = Event()

        def held_write(directory, config):
            write_entered.set()
            if not release_write.wait(timeout=5):
                raise AssertionError("Settings save was not released")
            return write_config(directory, config)

        def observed_load(directory):
            toggle_loaded.set()
            return load_config(directory)

        def toggle():
            toggle_started.set()
            return toggle_autosave(self.directory)

        with patch("herdr_description.app.write_config", side_effect=held_write), patch(
            "herdr_description.config.load_config", side_effect=observed_load,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            save = executor.submit(poll, self.save_payload(original, '{"panel_width_percent": 45}'))
            try:
                self.assertTrue(write_entered.wait(timeout=5))
                toggled = executor.submit(toggle)
                self.assertTrue(toggle_started.wait(timeout=5))
                self.assertFalse(toggle_loaded.wait(timeout=0.1))
            finally:
                release_write.set()
            self.assertTrue(save.result(timeout=5)["saved"])
            self.assertFalse(toggled.result(timeout=5).autosave_enabled)
        config = load_config(self.directory)
        self.assertFalse(config.autosave_enabled)
        self.assertEqual(config.panel_width_percent, 45)
