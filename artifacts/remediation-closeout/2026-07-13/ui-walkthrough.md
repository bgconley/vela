# Live UI walkthrough

- Certified code SHA: `de9b0a13f2ef7166014794bb211845dd3ba96123`
- Browser surface: in-app Chromium against isolated `textual-serve` sessions
- Isolation root: `/tmp/vela-closeout-ui-de9b0a1` (removed after verification)
- Main viewport mapping: 1283x813 browser pixels to 142x38 terminal cells
- Responsive mappings: 904x556 to 100x26; 723x513 to 80x24
- Verdict: PASS for every exercised criterion

All screenshots are raw browser captures. The terminal canvas occupies the upper-left portion of
the browser viewport because the served terminal is cell-sized; the captures were not cropped or
rescaled after collection.

| Shot | Acceptance evidence | Result |
|---|---|---|
| `01-first-run-142x38.jpeg` | Pristine first-run has honest empty state and quick-start guidance | PASS |
| `02-help-142x38.jpeg` | Human Help title, key groups, and readable marker legend | PASS |
| `03-dashboard-142x38.jpeg` | Wide idle dashboard, adaptive chrome/sidebar/footer | PASS |
| `04-target-manager.jpeg` | Full-width Target Manager with live detail card | PASS |
| `05-dead-target-connecting.jpeg` | Hung SSH switch shows amber `connecting…`; TUI remains responsive | PASS |
| `06-dead-target-cancelled-local-stable.jpeg` | Mid-connect escape returns to coherent local state | PASS |
| `07-model-manager.jpeg` | Full-width Model Manager and readable row grammar | PASS |
| `08-build-manager.jpeg` | Full-width Build Manager and honest empty actions | PASS |
| `09-flag-manager-checkbox-off.jpeg` | Flag Manager title-first layout and literal `[ ]` | PASS |
| `10-flag-manager-checkbox-on-stable.jpeg` | Flag Manager literal `[✓]` after toggle | PASS |
| `11-wizard-step1.jpeg` | Deployment composer target step | PASS |
| `12-wizard-runtime.jpeg` | Runtime step and keyboard-forward flow | PASS |
| `13-wizard-model.jpeg` | Model step without dead-end focus | PASS |
| `14-wizard-pinned-model.jpeg` | Existing pinned model selected | PASS |
| `15-wizard-customize.jpeg` | Customize step remains readable at the wide viewport | PASS |
| `17-wizard-review-command-sha.jpeg` | Review command includes resolved immutable SHA and field provenance | PASS |
| `18-wizard-saved.jpeg` | Wizard saves and returns cleanly | PASS |
| `19-config-picker.jpeg` | Config Picker content and selection | PASS |
| `20-config-picker-no-match.jpeg` | No-match state stays open with an honest escape hint | PASS |
| `21-fake-launch-progress.jpeg` | Run separator and phase timing during fake launch | PASS |
| `22-fake-ready.jpeg` | Green READY state after health/model gate | PASS |
| `23-fake-stopped-operator-stable.jpeg` | Terminal phase and `STOPPED by operator` closure | PASS |
| `24-dashboard-100x26.jpeg` | 100-column adaptive dashboard without collision or clipping | PASS |
| `25-dashboard-80x24.jpeg` | 80-column adaptive dashboard without collision or clipping | PASS |
| `26-pin-handoff-checkbox-off.jpeg` | Dedicated pin handoff renders literal `[ ]` checkbox grammar | PASS |
| `27-pin-handoff-checkbox-on.jpeg` | Dedicated pin handoff renders literal `[✓]` after toggle | PASS |
| `28-pin-handoff-cancel-return.jpeg` | Escape returns directly to the wizard model step | PASS |
| `29-pin-handoff-no-refire.jpeg` | Same wizard state remains after a wait; handoff does not re-fire | PASS |
| `30-pin-handoff-filled-current-sha.jpeg` | Filled Pin HF form from a pristine exact-SHA worktree | PASS |
| `31-pin-handoff-submitting-current-sha.jpeg` | Submission paints a visible `pinning model…` busy state | PASS |
| `32-pin-handoff-success-return-current-sha.jpeg` | Successful handoff returns once with four completed wizard steps | PASS |
| `33-pin-handoff-review-sha-current.jpeg` | Returned wizard composes immutable SHA and per-field provenance | PASS |

The successful handoff supplement ran from a pristine detached worktree at the certified SHA;
its isolated daemon reported `v0.1.0-108-gde9b0a1`. See `pin-handoff-success.md` for the registry,
source, and cleanup proof.

The walkthrough did not reproduce bugs 233 through 240. The earlier cosmetic aborted-switch
finding, bug-307, was not accepted as harmless: the code now binds every target-switch and
keepalive completion to both its starting generation and client identity. The dead-target exercise
returned to local without a stale banner or wrong-transport remediation.

Cleanup stopped every isolated agent through its explicit socket, closed or detached the browser
tabs, stopped every serving process, removed both isolation roots and the pristine detached
worktree, and verified no fake child or port 8765/8815 listener remained. The real local daemon and
Blackbird's shared daemon were not used by this walkthrough.
