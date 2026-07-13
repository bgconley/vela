# Live incident: Review provenance markup crash

- Observed: 2026-07-13 during the visible Oxcart controller=target walkthrough.
- Code under test: `6c8845e2c7d933b863aaf3acf097ea097fb05b25`.
- Trigger: select the checked-in Oxcart recipe, pin `Qwen/Qwen3.6-27B-FP8` at commit `e89b16ebf1988b3d6befa7de50abc2d76f26eb09`, return to the wizard, then press `Ctrl+S` on Review.
- Result: Textual terminated the client session with `MarkupError` while rendering `#new-deployment-review-derived`. The failing provenance value was the recipe's bracketed Docker `extra_run_args` JSON ending in `--label", "ai.vela.profile=oxcart-qwen36-27b-fp8-mtp-vl"]`.
- Safety state immediately after failure: no matching Vela container, no listener on port 18004, and no GPU compute process.
- Visible evidence: `screenshots/07-pin-result.jpg` followed by `screenshots/08-session-ended.jpg` (Textual-Serve session-ended screen).
- Machine evidence: `remote-evidence/textual-serve.log` contains the live traceback; `remote-evidence/guard-postflight.json` records `"ok": true` after the owned server, tunnel, and daemon were stopped. The run-owned remote worktree and root were then removed.

This invalidates the prior claim that the full human wizard Review path was complete at the code-under-test SHA. The defect must receive a strategic regression test and a new proof SHA before the release walkthrough continues.
