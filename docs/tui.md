# Vela TUI key reference

[Documentation home](index.md) · [Getting started](getting-started.md) · [Operations](operations.md) · [Troubleshooting](troubleshooting.md)

Generated from the TUI's declared key bindings by `scripts/gen_tui_docs.py` — do not edit by hand. Regenerate after changing any screen's `BINDINGS`; `tests/test_docs.py::test_tui_doc_matches_bindings` fails if this file drifts from the code.

The dashboard footer advertises a state-filtered subset of these keys (control keys only during a run, log keys only when a log is present, and so on), but every binding below still works even when its footer hint is hidden.

Scope: this reference covers app- and screen-level bindings only. Widget-level bindings (for example the New Deployment wizard's preset chips, whose arrow keys move the cursor and `enter` selects) are handled inside their widgets and are out of scope here.

## Dashboard (root app bindings)

| Key | Action | Description |
| --- | --- | --- |
| `l`, `enter` | `load` | Launch |
| `s` | `stop` | Stop |
| `K` | `kill` | Kill |
| `r` | `restart` | Restart |
| `n` | `new_deployment` | New |
| `c` | `config_picker` | Configs |
| `t` | `targets` | Targets |
| `b` | `builds` | Builds |
| `F` | `flags` | Flags |
| `m` | `models` | Models |
| `R` | `reconnect` | Reconnect |
| `/` | `search` | Search |
| `f` | `filter` | Filter |
| `p` | `pause` | Pause |
| `w` | `wrap` | Wrap |
| `g` | `top` | Top |
| `G` | `bottom` | Bottom |
| `tab` | `focus_next` | Focus |
| `?` | `help` | Help |
| `f1` | `help` | Help |
| `q` | `quit` | Quit |
| `ctrl+c` | `quit` | Quit |

## Adopt Build

| Key | Action | Description |
| --- | --- | --- |
| `escape` | `cancel` | Cancel |

## Build Manager

| Key | Action | Description |
| --- | --- | --- |
| `up` | `previous` | Previous |
| `down` | `next` | Next |
| `enter` | `accept` | Select |
| `n` | `new` | New |
| `a` | `adopt` | Adopt |
| `v` | `verify` | Verify |
| `P` | `pin_config` | Pin to config |
| `r` | `repair` | Repair |
| `F` | `flags` | Flags |
| `x` | `remove` | Remove |
| `escape` | `cancel` | Cancel |

## Config Picker

| Key | Action | Description |
| --- | --- | --- |
| `up` | `previous` | Previous |
| `down` | `next` | Next |
| `enter` | `accept` | Select |
| `ctrl+t` | `push` | Push to target |
| `escape` | `cancel` | Cancel |

## Confirm

| Key | Action | Description |
| --- | --- | --- |
| `enter` | `confirm` | Confirm |
| `s` | `stop` | Stop |
| `K` | `kill` | Kill |
| `escape`, `c` | `cancel` | Cancel |

## Create Build

| Key | Action | Description |
| --- | --- | --- |
| `escape` | `cancel` | Cancel |
| `ctrl+g` | `install_uv` | Install uv |

## Download Model

| Key | Action | Description |
| --- | --- | --- |
| `escape` | `cancel` | Cancel |
| `ctrl+r` | `toggle_raw` | Raw patterns |

## Flag Manager

| Key | Action | Description |
| --- | --- | --- |
| `up` | `previous` | Previous |
| `down` | `next` | Next |
| `d` | `reset_default` | Reset |
| `p` | `reset_preset` | Preset |
| `x` | `toggle_changed_only` | Changed |
| `ctrl+s` | `save` | Save |
| `escape` | `cancel` | Close |

## Help

| Key | Action | Description |
| --- | --- | --- |
| `escape` | `close` | Close |
| `?` | `close` | Close |
| `f1` | `close` | Close |

## Log Prompt

| Key | Action | Description |
| --- | --- | --- |
| `escape` | `cancel` | Cancel |

## Model Manager

| Key | Action | Description |
| --- | --- | --- |
| `up` | `previous` | Previous |
| `down` | `next` | Next |
| `enter` | `accept` | Use once |
| `d` | `download` | Download |
| `p` | `pin` | Pin |
| `r` | `refresh_models` | Refresh |
| `v` | `verify` | Verify |
| `x` | `remove` | Remove |
| `escape` | `cancel` | Close |

## New Deployment

| Key | Action | Description |
| --- | --- | --- |
| `ctrl+n` | `next_step` | Next |
| `ctrl+b` | `previous_step` | Back |
| `enter` | `advance_or_submit` | Next |
| `ctrl+r` | `toggle_advanced` | Advanced |
| `ctrl+s` | `submit` | Review |
| `escape` | `cancel` | Cancel |

## New Deployment Review

| Key | Action | Description |
| --- | --- | --- |
| `b` | `back` | Back |
| `f` | `customize` | Flags |
| `s` | `save_smoke` | Smoke |
| `ctrl+s` | `save` | Save |
| `escape` | `cancel` | Cancel |

## Pin Model

| Key | Action | Description |
| --- | --- | --- |
| `escape` | `cancel` | Cancel |
| `ctrl+s` | `submit` | Pin |
| `ctrl+r` | `toggle_advanced` | Advanced |

## Target Edit

| Key | Action | Description |
| --- | --- | --- |
| `escape` | `cancel` | Cancel |

## Target Manager

| Key | Action | Description |
| --- | --- | --- |
| `up` | `previous` | Previous |
| `down` | `next` | Next |
| `enter` | `accept` | Select |
| `n` | `new` | New |
| `e` | `edit` | Edit |
| `b` | `bootstrap` | Bootstrap |
| `p` | `push_config` | Push config |
| `R` | `reconnect` | Reconnect |
| `x` | `remove` | Remove |
| `v` | `view_capabilities` | View all capabilities |
| `escape` | `cancel` | Cancel |

## Related documentation

- [Illustrated first deployment](tutorials/first-deployment.md)
- [Day-two operations](operations.md)
- [Complete CLI reference](cli-reference.md)
- [Troubleshooting by symbolic error](troubleshooting.md)
