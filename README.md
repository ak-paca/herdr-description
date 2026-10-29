# herdr-description

<img src="example.png" alt="Space, tab, and pane descriptions in a Herdr session" width="400">

Persistent descriptions for Herdr spaces, tabs, and panes. Requires Herdr 0.9.3+ and Python 3.9+ with `curses`. No third-party dependencies.

Descriptions open as stacked native panes in a full-height column on the right. Type in the editor; there are no Vim modes. Each text modification saves automatically by default.

When regrouping an existing split layout, the plugin briefly moves panes through a temporary tab. Running terminals and editors, pane IDs, and split directions and proportions are preserved.

## Install

```sh
herdr plugin install ak-paca/herdr-description
```

Generate the keybinding block from the plugin directory:

```sh
python3 plugin.py keybindings
```

Paste the marked block into Herdr's `config.toml`, then:

```sh
herdr server reload-config
```

`prefix` is normally `Ctrl+B`. Actions are also in Herdr's action palette.

### From source (optional)

```sh
cd /path/to/herdr-description
herdr plugin link "$PWD"
herdr plugin enable herdr-description
```

## Editor

New descriptions start with `-- `, with the cursor ready for a name. A non-empty first line beginning with `--` renames the associated workspace, tab, or pane as it is saved:

```text
-- new workspace name
comment line
```

Only that description's own header controls its object's name. A blank `-- ` does not blank the name or count as a non-empty description. Removing the header stops controlling the name; the last explicit name remains, and pane metadata from other reporters can become visible again. Description indicators still inherit upward and compose with header names.

`Ctrl+A` toggles per-modification autosave for **all description editors using the same plugin configuration**, including named Herdr sessions. The setting survives restarts. With it off, changes still save on focus loss or `Ctrl+S`. The settings editor always saves manually so incomplete JSON is not applied. Conflicting external edits remain protected; automatic saves never force an overwrite.

The footer shows status and a help hint. `Ctrl+/` opens the commands helper; press `q` to close it. `Ctrl+_` or `F1` also opens help when the terminal does not distinguish `Ctrl+/`.

| Shortcut | Behavior |
|---|---|
| `Ctrl+A` | Toggle global per-modification autosave |
| `Ctrl+/` / `F1` | Open commands helper; `q` closes it |
| `Ctrl+S` | Save |
| `Ctrl+R` | Reload (confirms if dirty) |
| `Ctrl+K` | Delete current line |
| `Ctrl+L` | Clear all text (`Y` confirms) |
| `Ctrl+W` | Toggle wrapping for both text and the footer |
| `Ctrl+X` | Remove this description, its file and mapping, and close its editor |
| Mouse drag / Shift+arrows | Select text |
| `Ctrl+Space`, then arrows | Start keyboard selection |
| `Ctrl+C` | Copy the selected text |
| Escape | Clear the selection |

`Ctrl+X` acts on the description open in that editor, independently of the last focused work pane. It is disabled in the settings editor.

HTTP and HTTPS links are underlined. Ctrl+click opens the complete address, including paths, query parameters, and fragments, even when the link is wrapped or partly off-screen. Copy uses Herdr's terminal clipboard forwarding; remote or outer terminals may require clipboard permission.

## Plugin commands

| Action | ID | Default |
|---|---|---|
| Create/edit tab | `edit-tab` | `prefix+alt+t` |
| Create/edit space | `edit-space` | `prefix+alt+s` |
| Create/edit pane | `edit-pane` | `prefix+alt+p` |
| Toggle description indicators | `toggle-indicators` | `prefix+alt+i` |
| Remove space | `remove-space` | `prefix+alt+shift+s` |
| Remove tab | `remove-tab` | `prefix+alt+shift+t` |
| Remove pane | `remove-pane` | `prefix+alt+shift+p` |

Change bindings in the plugin `hotkeys` config, regenerate the block, and reload. See [config.example.json](config.example.json).

## FAQ

**Where are config and description files?**

```sh
herdr plugin config-dir herdr-description
python3 plugin.py paths
```

For custom locations, pass `--config-dir` and `--state-dir` to `plugin.py`.

**How do I open settings?**

```sh
herdr plugin action invoke herdr-description.settings
```

`panel_width_percent` defaults to `30` (15–60). `autosave_enabled` defaults to `true` and is updated by `Ctrl+A`. `hotkeys` maps action IDs to chords.

**How do I restore, restart, or export?**

```sh
herdr plugin action invoke herdr-description.restore
herdr plugin action invoke herdr-description.restart-editors
python3 plugin.py export > descriptions-export.json
herdr plugin log list --plugin herdr-description
```

Pass `--session <name>` to `herdr` for a named session. Use `restore` to repair a partial-height description column. If a layout operation is interrupted, its recovery plan is retained and running terminals stay open; `restore` retries the operation.

**How do I disable or uninstall?**

Toggle description indicators off first if they are enabled, so name markers are removed.

```sh
herdr plugin disable herdr-description
herdr plugin uninstall herdr-description
herdr plugin unlink herdr-description
```

Remove the marked keybinding block from `config.toml` and run `herdr server reload-config`.

**How do I run tests?**

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Reference: [plugins](https://herdr.dev/docs/plugins/), [configuration](https://herdr.dev/docs/configuration/), [Herdr 0.9.3](https://github.com/herdrdev/herdr/tree/v0.9.3).
