import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from herdr_description.storage import DescriptionStore


class CliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        self.state_dir = self.root / "state"
        self.environment = dict(os.environ)
        self.environment.update(
            {
                "HERDR_PLUGIN_CONFIG_DIR": str(self.config_dir),
                "HERDR_PLUGIN_STATE_DIR": str(self.state_dir),
                "XDG_STATE_HOME": str(self.root / "xdg-state"),
            }
        )
        self.environment.pop("HERDR_SOCKET_PATH", None)
        self.plugin = Path(__file__).resolve().parents[1] / "plugin.py"

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, str(self.plugin), *map(str, arguments)],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def invalid_config(self):
        content = '{ "editor": ["vim"], invalid JSON\n'
        (self.config_dir / "config.json").write_text(content, encoding="utf-8")
        return content

    def create_description(self, state_dir, content):
        with DescriptionStore(state_dir) as store:
            return store.create("session", "space", "space", content=content)

    def test_export_recovers_descriptions_with_malformed_configuration(self):
        invalid = self.invalid_config()
        description = self.create_description(self.state_dir, "recover this description")
        result = self.run_cli("export")
        self.assertEqual(result.returncode, 0, result.stderr)
        records = json.loads(result.stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["description_id"], description.description_id)
        self.assertEqual(records[0]["content"], description.content)
        self.assertEqual(
            (self.config_dir / "config.json").read_text(encoding="utf-8"), invalid
        )

    def test_paths_works_with_malformed_configuration(self):
        invalid = self.invalid_config()
        result = self.run_cli("paths")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "config": str(self.config_dir / "config.json"),
                "state": str(self.state_dir),
            },
        )
        self.assertEqual(
            (self.config_dir / "config.json").read_text(encoding="utf-8"), invalid
        )

    def test_explicit_config_and_state_directories_override_environment(self):
        override_config = self.root / "override-config"
        override_state = self.root / "override-state"
        result = self.run_cli(
            "--config-dir", override_config, "--state-dir", override_state, "paths"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "config": str(override_config / "config.json"),
                "state": str(override_state),
            },
        )
        self.assertFalse((override_config / "config.json").exists())

    def test_config_override_preserves_environment_state_directory(self):
        override_config = self.root / "override-config"
        result = self.run_cli("paths", "--config-dir", override_config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "config": str(override_config / "config.json"),
                "state": str(self.state_dir),
            },
        )

    def test_export_state_override_reads_only_selected_database(self):
        self.invalid_config()
        self.create_description(self.state_dir, "environment database")
        override_state = self.root / "override-state"
        selected = self.create_description(override_state, "selected database")
        result = self.run_cli("export", "--state-dir", override_state)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = json.loads(result.stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["description_id"], selected.description_id)
        self.assertEqual(records[0]["content"], "selected database")

    def test_invalid_settings_are_reported_without_replacing_file(self):
        invalid = self.invalid_config()
        result = self.run_cli("keybindings")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Invalid configuration", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(
            (self.config_dir / "config.json").read_text(encoding="utf-8"), invalid
        )

    def test_paths_does_not_require_existing_configuration(self):
        result = self.run_cli("paths")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], str(self.state_dir))
        self.assertFalse((self.config_dir / "config.json").exists())


if __name__ == "__main__":
    unittest.main()
