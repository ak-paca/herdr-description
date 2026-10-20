# herdr-description

<img src="example.png" alt="Space, tab, and pane descriptions in a Herdr session" width="800">

Persistent descriptions for Herdr spaces, tabs, and panes. Requires Herdr 0.9.3+ and Python 3.9+ with `curses`. No third-party dependencies.

Descriptions open as stacked native panes in a full-height column on the right, beside the tab's entire work layout. Type in the editor; there are no Vim modes. Changes save on focus loss or `Ctrl+S`.

When regrouping an existing split layout, the plugin briefly moves panes through a temporary tab in the same space. Running terminals and editors, pane IDs, and the work layout's split directions and proportions are preserved. The temporary tab disappears when the panes return.

| Shortcut | Behavior |
|---|---|
| `Ctrl+S` | Save |
| `Ctrl+R` | Reload (confirms if dirty) |
| `Ctrl+K` | Delete current line |
| `Ctrl+L` | Clear all text (`Y` confirms) |
| `Ctrl+W` | Toggle word wrap |
| `Ctrl+X` | Remove this description, its file and mapping, and close its editor |

`Ctrl+X` acts on the description open in that editor, independently of the last focused work pane. It is disabled in the settings editor.

## Install

```sh
cd /path/to/herdr-description
herdr plugin link "$PWD"
herdr plugin enable herdr-description
herdr plugin action list --plugin herdr-description
```

```sh
herdr plugin action invoke herdr-description.edit-tab
```

## Hotkeys

```sh
python3 plugin.py keybindings
```

Paste the marked block into Herdr's `config.toml`, then:

```sh
herdr server reload-config
```

| Action | ID | Default |
|---|---|---|
| Create/edit tab | `edit-tab` | `prefix+alt+t` |
| Create/edit space | `edit-space` | `prefix+alt+s` |
| Create/edit pane | `edit-pane` | `prefix+alt+p` |
| Remove space | `remove-space` | `prefix+alt+shift+s` |
| Remove tab | `remove-tab` | `prefix+alt+shift+t` |
| Remove pane | `remove-pane` | `prefix+alt+shift+p` |
| Settings | `settings` | unbound |
| Close pane (confirm) | `close-pane` | unbound |
| Close tab (confirm) | `close-tab` | unbound |
| Close space (confirm) | `close-space` | unbound |

`prefix` is normally `Ctrl+B`. Change bindings in the plugin `hotkeys` config, regenerate the block, and reload. `restore` is in the action palette.

To route Herdr close shortcuts through confirmation, set plugin hotkeys:

```json
{
  "close-pane": "prefix+x",
  "close-tab": "prefix+shift+x",
  "close-space": "prefix+shift+d"
}
```

Clear the built-in bindings in `[keys]`:

```toml
close_pane = ""
close_tab = ""
close_workspace = ""
```

Then regenerate and reload.

## Settings and paths

```sh
herdr plugin action invoke herdr-description.settings
herdr plugin config-dir herdr-description
python3 plugin.py paths
```

See [config.example.json](config.example.json). Defaults: `panel_width_percent` is `30` (15–60); `hotkeys` maps action IDs to chords.

## Restart, restore, export

```sh
herdr plugin action invoke herdr-description.restart-editors
herdr plugin action invoke herdr-description.restore
python3 plugin.py export > descriptions-export.json
herdr plugin log list --plugin herdr-description
```

Pass `--session <name>` to `herdr` when targeting a named session. For custom locations, pass `--config-dir` and `--state-dir` to `plugin.py`.

Use `restore` to repair an existing partial-height description column. If a layout operation is interrupted, its recovery plan is retained and running terminals stay open; `restore` retries the operation.

## Disable or uninstall

```sh
herdr plugin disable herdr-description
herdr plugin unlink herdr-description
herdr plugin uninstall herdr-description
```

Remove the marked keybinding block from `config.toml` and run `herdr server reload-config`.

## Development

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Reference: [plugins](https://herdr.dev/docs/plugins/), [configuration](https://herdr.dev/docs/configuration/), [Herdr 0.9.3](https://github.com/herdrdev/herdr/tree/v0.9.3).
