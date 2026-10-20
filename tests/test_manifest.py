import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("herdr"), "Herdr is not installed")
class ManifestTest(unittest.TestCase):
    def test_installed_herdr_accepts_manifest_without_warnings(self):
        with tempfile.TemporaryDirectory(prefix="hdm.", dir="/tmp") as temporary:
            root = Path(temporary)
            config = root / "config/herdr/config.toml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "[update]\nversion_check = false\nmanifest_check = false\n",
                encoding="utf-8",
            )
            environment = {
                key: value for key, value in os.environ.items()
                if not key.startswith("HERDR_")
            }
            environment.update(
                XDG_CONFIG_HOME=str(root / "config"),
                XDG_STATE_HOME=str(root / "state"),
                HERDR_CONFIG_PATH=str(config),
            )
            result = subprocess.run(
                [shutil.which("herdr"), "--session", "manifest-test", "plugin", "link", str(ROOT)],
                env=environment, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plugin = json.loads(result.stdout)["result"]["plugin"]
            self.assertEqual(plugin["plugin_id"], "herdr-description")
            self.assertEqual(plugin.get("warnings", []), [])
            self.assertNotIn("save", {action["id"] for action in plugin["actions"]})
