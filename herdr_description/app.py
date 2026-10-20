import argparse
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from .api import Herdr, HerdrError
from .config import Config, ConfigError, keybindings, load_config, parse_config, write_config
from .controller import App, PLUGIN_ID, context
from .documents import DocumentStore
from .editor import run_editor
from .storage import DescriptionStoreError


ROOT = Path(__file__).resolve().parent.parent


def paths():
    config = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    state = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not config:
        result = subprocess.run(
            [os.environ.get("HERDR_BIN_PATH", "herdr"), "plugin", "config-dir", PLUGIN_ID],
            check=True, capture_output=True, text=True,
        )
        config = result.stdout.strip()
    if not state:
        state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        state = str(state_home / "herdr/plugins" / PLUGIN_ID)
    return Path(config), Path(state)


def edit_settings(config_dir):
    path = config_dir / "config.json"

    def poll(payload):
        content = path.read_bytes().decode("utf-8")
        save = payload.get("save")
        if save is not None:
            try:
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if digest != save.get("expected_digest") and not save.get("force"):
                    return {"error": "Settings changed in another editor.", "conflict": True}
                write_config(config_dir, parse_config(save["content"]))
                content = path.read_bytes().decode("utf-8")
            except (OSError, ValueError) as error:
                return {"error": str(error)}
        return {
            "record": {"content": content, "title": "herdr-description settings", "focused": False},
            "saved": save is not None,
        }

    run_editor(path, poll)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Persistent space, tab and pane descriptions for Herdr.")
    parser.add_argument(
        "command", choices=[
            "action", "event", "restore", "panel", "tick", "settings",
            "confirm-close", "keybindings", "paths", "export",
        ],
    )
    parser.add_argument("name", nargs="?")
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--section")
    args = parser.parse_args()
    if args.command in ("action", "tick") and args.name is None:
        parser.error(args.command + " requires a name")
    try:
        if args.config_dir:
            config_dir = args.config_dir
            default_state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "herdr/plugins" / PLUGIN_ID
            state_dir = Path(os.environ.get("HERDR_PLUGIN_STATE_DIR", default_state))
        else:
            config_dir, state_dir = paths()
        if args.state_dir:
            state_dir = args.state_dir
        if args.command == "paths":
            print(json.dumps({"config": str(config_dir / "config.json"), "state": str(state_dir)}, indent=2))
            return
        if args.command == "export":
            with DocumentStore(state_dir) as store:
                records = [dataclasses.asdict(record) for record in store.export_descriptions()]
            print(json.dumps(records, ensure_ascii=False, indent=2))
            return
        try:
            config = load_config(config_dir)
        except ConfigError:
            if args.command == "settings" or (args.command == "action" and args.name == "settings"):
                config = Config()
            else:
                raise
        if args.command == "keybindings":
            print(keybindings(config), end="")
            return
        if args.command == "settings":
            edit_settings(config_dir)
            return
        if args.command == "action" and args.name == "settings":
            api = Herdr()
            _, _, pane_id = context(api.snapshot())
            api.call(
                "plugin.pane.open", plugin_id=PLUGIN_ID,
                entrypoint="settings", placement="overlay",
                target_pane_id=pane_id, focus=True,
            )
            return
        if args.command == "panel":
            if not os.environ.get("DESCRIPTION_SECTION_ID"):
                raise ValueError("Relink the plugin and reopen descriptions to use separate editors.")
            if not os.environ.get("DESCRIPTION_FILE"):
                raise ValueError("The description file is missing from the editor context.")
            app = App(config_dir, state_dir)
            try:
                def poll(payload):
                    try:
                        return app.section_tick(
                            os.environ["DESCRIPTION_TAB_ID"],
                            os.environ["DESCRIPTION_SECTION_ID"], payload,
                        )
                    except (OSError, ValueError, HerdrError, DescriptionStoreError) as error:
                        return {"error": str(error)}

                run_editor(Path(os.environ["DESCRIPTION_FILE"]), poll)
                app.close_editor(
                    os.environ["DESCRIPTION_TAB_ID"],
                    os.environ["DESCRIPTION_SECTION_ID"],
                    os.environ["DESCRIPTION_VIEW_LABEL"],
                )
            finally:
                app.store.close()
            return
        app = App(config_dir, state_dir)
        try:
            if args.command == "action":
                app.action(args.name)
            elif args.command == "event":
                app.event(json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON", "{}")))
            elif args.command == "restore":
                app.event(
                    {}, startup=True,
                    verify_identities=os.environ.get("HERDR_PLUGIN_EVENT") == "startup",
                )
            elif args.command == "tick":
                payload = json.load(sys.stdin)
                if args.section:
                    result = app.section_tick(args.name, args.section, payload)
                else:
                    result = app.tick(args.name, payload)
                print(json.dumps(result, ensure_ascii=False))
            elif args.command == "confirm-close":
                app.confirm_close()
        finally:
            app.store.close()
    except (OSError, ValueError, HerdrError, DescriptionStoreError, subprocess.SubprocessError) as error:
        if args.command == "tick":
            print(json.dumps({"error": str(error)}))
        else:
            print(PLUGIN_ID + ": " + str(error), file=sys.stderr)
            sys.exit(1)
