# Successful live Pin HF handoff

- Certified code SHA: `de9b0a13f2ef7166014794bb211845dd3ba96123`
- Source tree: pristine detached worktree `/tmp/vela-ui-code-de9b0a1`
- Source worktree status before and after: empty porcelain output
- UI isolation root: `/tmp/vela-closeout-pin-success-de9b0a1`
- Browser terminal: 134x34 cells
- Result: PASS

The browser drove a pristine empty Vela state through New Deployment target and runtime steps,
selected `Pin HF repo`, entered `facebook/opt-125m`, submitted the dedicated handoff, returned to
the wizard exactly once, and composed the Review screen. The screenshots retain each material
state:

- `screenshots/30-pin-handoff-filled-current-sha.jpeg` — filled dedicated pin form.
- `screenshots/31-pin-handoff-submitting-current-sha.jpeg` — visible `pinning model…` busy state.
- `screenshots/32-pin-handoff-success-return-current-sha.jpeg` — successful return to the wizard's
  Review step, with Target, Runtime, Model, and Customize complete.
- `screenshots/33-pin-handoff-review-sha-current.jpeg` — composed review with per-field provenance
  and immutable revision `27dcfa74d334bc871f3234de431e71c6eeba5dd6` in the resolved command.

The isolated daemon proved the live app was running the pristine source tree rather than the dirty
evidence checkout:

```json
{"pid": 68932, "revision": "v0.1.0-108-gde9b0a1", "socket_path": "/tmp/vela-closeout-pin-success-de9b0a1/agent/agent.sock", "start_ts": "2026-07-13T08:38:36.444094Z", "status": "running", "version": "0.1.0"}
```

The isolated registry immediately after the handoff contained one pinned entry:

```json
{"cache_state":"cached","commit_sha":"27dcfa74d334bc871f3234de431e71c6eeba5dd6","display_name":"facebook/opt-125m","entry_id":"01KXDA3A55T9ECMHXXZEP1CYZQ","pinned":true,"repo_id":"facebook/opt-125m","revision":"27dcfa74d334bc871f3234de431e71c6eeba5dd6","source":"hf_repo"}
```

No download-now action or model launch was requested. Cleanup stopped PID 68932 through only its
explicit socket, detached the browser, stopped the serving process, verified port 8815 and the
socket were free, removed both temporary roots, and removed the detached worktree. The real local
daemon was untouched.
