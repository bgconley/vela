# Vela Visual QA

- Generated: `2026-06-13T00:46:01.444329Z`
- Figma reference: https://www.figma.com/design/9xUgzyoFqWmd40tV5dwaHv/vLLM-TUI-Loader-Screens---Canonical-v2?node-id=22-2
- Downloaded reference: `figma-node-22-2.png`
- Method: Textual headless SVG capture with placeholder-only state.
- Verdict: layout/anatomy pass with one exporter limitation. The generated
  modal panels now keep compact Figma-like height instead of stretching to the
  viewport. Runtime imports still force Textual truecolor, but Textual's SVG
  export quantizes the palette to terminal grays, so these SVGs are layout
  evidence and not a final color-proof artifact.

## Captured Screens

- `wide-144x42-dashboard.svg`
- `wide-144x42-config-picker.svg`
- `wide-144x42-new-deployment.svg`
- `wide-144x42-build-manager.svg`
- `wide-144x42-model-manager.svg`
- `wide-144x42-target-manager.svg`
- `wide-144x42-help-modal.svg`
- `wide-144x42-log-prompt-modal.svg`
- `wide-144x42-confirm-modal.svg`
- `wide-144x42-target-edit-modal.svg`
- `standard-120x36-dashboard.svg`
- `standard-120x36-config-picker.svg`
- `standard-120x36-new-deployment.svg`
- `standard-120x36-build-manager.svg`
- `standard-120x36-model-manager.svg`
- `standard-120x36-target-manager.svg`
- `standard-120x36-help-modal.svg`
- `standard-120x36-log-prompt-modal.svg`
- `standard-120x36-confirm-modal.svg`
- `standard-120x36-target-edit-modal.svg`
- `compact-80x24-dashboard.svg`
- `compact-80x24-config-picker.svg`
- `compact-80x24-new-deployment.svg`
- `compact-80x24-build-manager.svg`
- `compact-80x24-model-manager.svg`
- `compact-80x24-target-manager.svg`
- `compact-80x24-help-modal.svg`
- `compact-80x24-log-prompt-modal.svg`
- `compact-80x24-confirm-modal.svg`
- `compact-80x24-target-edit-modal.svg`

## Review Notes

- Compared against the Canonical v2 board screenshot from Figma node `22:2`.
- Confirm/log/help/target-edit modal scale matched the small-modal intent after
  adding `height: auto` with max-height caps.
- Dashboard and manager captures are placeholder-state layout checks. They do
  not exercise every colored READY/ERROR/TIMEOUT state shown in the Figma board.
- Color-token comparison remains limited by `save_screenshot` SVG export; use
  an interactive terminal or browser-backed capture for final color sign-off.
