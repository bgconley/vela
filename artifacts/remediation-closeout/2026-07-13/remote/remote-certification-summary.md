# Safe Manual Remote Certification

- Certified code SHA: `de9b0a13f2ef7166014794bb211845dd3ba96123`
- Published branch: `remediate/2026-07-09-review`
- Controller: `bgconley@10.25.0.50` (`oxcart`)
- Controller source: `/home/bgconley/repos/lab-tui`
- Dedicated validation venv: `/tank/venvs/lab-tui-remediation-de9b0a1`
- Isolated daemon runtime: `/tank/venvs/lab-tui-remediation-de9b0a1/agent-runtime`
- Blackbird validation scope: handshake only; no model, build, config, smoke, or resume operation

## Baseline command

```bash
env \
  VELA_REMOTE_BRANCH='remediate/2026-07-09-review' \
  VELA_REMOTE_EXPECTED_SHA='de9b0a13f2ef7166014794bb211845dd3ba96123' \
  VELA_REMOTE_VENV='/tank/venvs/lab-tui-remediation-de9b0a1' \
  VELA_REMOTE_AGENT_RUNTIME_DIR='/tank/venvs/lab-tui-remediation-de9b0a1/agent-runtime' \
  VELA_REMOTE_PYTEST_ARGS='-q tests/test_remote_workflow.py tests/test_transport_factory.py tests/test_targets.py' \
  VELA_REMOTE_TARGET='' \
  VELA_REMOTE_BUILD_SPEC='' \
  VELA_REMOTE_MODEL_REPO='' \
  VELA_REMOTE_MODEL_REF='' \
  VELA_REMOTE_MODEL_REVISION='' \
  VELA_REMOTE_REAL_RESUME_CONFIG='' \
  VELA_REMOTE_GATED_MODEL_REPO='' \
  VELA_REMOTE_ARTIFACT='1' \
  VELA_REMOTE_ARTIFACT_DIR='artifacts/remediation-closeout/2026-07-13/remote' \
  VELA_REMOTE_ARTIFACT_NAME='manual-remote-validation-de9b0a13.md' \
  VELA_REMOTE_TIMEOUT='1800' \
  VELA_SSH_OPTS='-o BatchMode=yes -o ConnectTimeout=15' \
  scripts/run_remote_tests.sh \
    bgconley@10.25.0.50 /home/bgconley/repos/lab-tui
```

Result: exit `0`. The owned worktree was detached at the requested SHA and the
artifact contains this fail-closed marker:

```text
REMOTE_REVISION_OK expected=de9b0a13f2ef7166014794bb211845dd3ba96123 actual=de9b0a13f2ef7166014794bb211845dd3ba96123 branch=remediate/2026-07-09-review source=owned-worktree
```

The remote Ruff gate passed, the safe slice passed `163/163` tests, and both
controller-local fake probes closed successfully:

```text
DAEMON_RESTART_LIVE_RUN_OK run_id=daemon-restart-1b4a7698330a4e429c5f884d1b3b5cd7 port=37853 url=http://127.0.0.1:37853 returncode=0
DISCONNECT_RECONNECT_RESUME_OK run_id=disconnect-reconnect-3df7fff7d0d14d1b81667d88419b0d33 first_seq=1 resume_inode=1847861 resume_offset=34 url=http://127.0.0.1:53499 returncode=0
```

## Blackbird handshake

The exact requested JSON command was attempted with the package installed from
the certified SHA:

```bash
ssh -o BatchMode=yes bgconley@10.25.0.50 \
  '/tank/venvs/lab-tui-remediation-de9b0a1/bin/vela targets test blackbird --json'
```

It exited `2`: this CLI surface does not implement `--json`. The supported
non-destructive handshake was then run:

```bash
ssh -o BatchMode=yes bgconley@10.25.0.50 \
  '/tank/venvs/lab-tui-remediation-de9b0a1/bin/vela targets test blackbird'
```

It exited `0` and reported `blackbird ok`, protocol `1`, matching controller and
agent version `0.1.0`, one Blackwell GPU, and no active Vela build/model.

## Safety reconciliation

- Target registry SHA-256 before and after:
  `2027a11ec3141d5d5520040a4808bb69f1d0d5cad4f7bc8bdf571c109e60f781`.
- Blackbird workload before and after: PID `74425`, start ticks `96376413`,
  command SHA-256
  `c5cede33c1b9929e794343efd680500a6a2230f9a442d6d1fab3d1368d98eae0`.
- Blackbird GPU snapshot before and after: GPU UUID
  `GPU-83baa75e-044c-99f9-beb7-bc4139326445`, utilization `0`, memory
  `94308/97887 MiB`.
- Isolated controller daemon after the run: JSON status `not-running`; no socket,
  runtime process, or runtime files remained.
- No owned validation worktree, validation temp directory, fake child process,
  target-connect process, or listener on probe ports `37853` and `53499`
  remained.
- The reusable controller checkout stayed at its pre-existing SHA
  `88d18d897aabe87184a10575bcf8b52842ff20af`; only its fetched remote ref was
  advanced to the certified SHA.

The evidence supports a green safe manual remote lane at the certified SHA and
a green supported Blackbird handshake. It also records one interface gap:
`vela targets test` cannot currently emit JSON.
