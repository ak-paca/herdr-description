import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping


ACTION_TITLES = {
    "edit-tab": "Create/edit tab description",
    "edit-space": "Create/edit space description",
    "edit-pane": "Create/edit pane description",
    "remove-space": "Remove space description",
    "remove-tab": "Remove tab description",
    "remove-pane": "Remove pane description",
    "settings": "herdr-description settings",
    "close-pane": "Close pane with description confirmation",
    "close-tab": "Close tab with description confirmation",
    "close-space": "Close space with description confirmation",
}

DEFAULT_HOTKEYS = {
    "edit-tab": "prefix+alt+t",
    "edit-space": "prefix+alt+s",
    "edit-pane": "prefix+alt+p",
    "remove-space": "prefix+alt+shift+s",
    "remove-tab": "prefix+alt+shift+t",
    "remove-pane": "prefix+alt+shift+p",
    "settings": "",
    "close-pane": "",
    "close-tab": "",
    "close-space": "",
}


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    panel_width_percent: int = 30
    hotkeys: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_HOTKEYS))


def _canonical_chord(chord: str) -> str:
    parts = chord.lower().split("+")
    return "+".join(sorted(parts[:-1]) + parts[-1:])


def _validated(values: Mapping[str, Any]) -> Config:
    allowed = {
        "editor",
        "autosave_enabled",
        "autosave_interval_ms",
        "panel_width_percent",
        "hotkeys",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ConfigError("Unknown configuration fields: " + ", ".join(sorted(unknown)))
    config = Config()
    width = values.get("panel_width_percent", config.panel_width_percent)
    if type(width) is not int or not 15 <= width <= 60:
        raise ConfigError("panel_width_percent must be an integer from 15 to 60")
    config.panel_width_percent = width

    hotkeys = values.get("hotkeys", {})
    if not isinstance(hotkeys, dict):
        raise ConfigError("hotkeys must be an object mapping action IDs to chords")
    hotkeys = {action: chord for action, chord in hotkeys.items() if action != "save"}
    unknown_actions = set(hotkeys) - set(DEFAULT_HOTKEYS)
    if unknown_actions:
        raise ConfigError("Unknown hotkey actions: " + ", ".join(sorted(unknown_actions)))
    config.hotkeys.update(hotkeys)
    used = {}
    for action, chord in config.hotkeys.items():
        if (
            not isinstance(chord, str)
            or chord != chord.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in chord)
        ):
            raise ConfigError(f"Hotkey {action} must be a chord string or an empty string")
        if chord:
            canonical = _canonical_chord(chord)
            if canonical in used:
                raise ConfigError(f"Hotkeys {used[canonical]} and {action} use the same chord")
            used[canonical] = action
    return config


def _as_dict(config: Config) -> Dict[str, Any]:
    return {
        "panel_width_percent": config.panel_width_percent,
        "hotkeys": config.hotkeys,
    }


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(config_dir: Path, config: Config, create_only: bool) -> Path:
    config = _validated(_as_dict(config))
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = config_dir / "config.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=config_dir)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(_as_dict(config), output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if create_only:
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                return destination
        else:
            os.replace(temporary_path, destination)
        _sync_directory(config_dir)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination


def _unique_object(pairs: List[Any]) -> Dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_config(text: str) -> Config:
    try:
        values = json.loads(text, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as error:
        raise ConfigError(str(error)) from error
    if not isinstance(values, dict):
        raise ConfigError("Configuration must be a JSON object")
    return _validated(values)


def load_config(config_dir: Path) -> Config:
    config_path = config_dir / "config.json"
    if not config_path.exists():
        _write(config_dir, Config(), create_only=True)
    try:
        with config_path.open(encoding="utf-8") as source:
            return parse_config(source.read())
    except (ValueError, UnicodeError) as error:
        raise ConfigError(f"Invalid configuration in {config_path}: {error}") from error


def write_config(config_dir: Path, config: Config) -> Path:
    return _write(config_dir, config, create_only=False)


def keybindings(config: Config) -> str:
    config = _validated(_as_dict(config))
    lines = ["# BEGIN herdr-description keybindings"]
    for action, chord in config.hotkeys.items():
        if not chord:
            continue
        lines.extend(
            [
                "",
                "[[keys.command]]",
                "key = " + json.dumps(chord, ensure_ascii=False),
                'type = "plugin_action"',
                "command = " + json.dumps("herdr-description." + action),
                "description = " + json.dumps(ACTION_TITLES[action]),
            ]
        )
    lines.extend(["", "# END herdr-description keybindings", ""])
    return "\n".join(lines)
