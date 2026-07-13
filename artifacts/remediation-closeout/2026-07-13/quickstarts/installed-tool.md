# Hermetic installed-tool quickstart

- Verdict: PASS (supplemental branch certification)
- Certified source revision: `de9b0a13f2ef7166014794bb211845dd3ba96123`
- Published branch: `remediate/2026-07-09-review`
- Executed (UTC): `2026-07-13T07:52:50Z` through `2026-07-13T07:53:23Z`
- Working directory: `/tmp/vela-quickstart-installed-de9b0a13f2ef/work`
- Isolated root: `/tmp/vela-quickstart-installed-de9b0a13f2ef`
- Isolation: dedicated `HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`,
  `XDG_RUNTIME_DIR`, `XDG_CACHE_HOME`, `UV_TOOL_DIR`, and
  `UV_TOOL_BIN_DIR`; the TUI ran outside every repository checkout.

## Installation

Command (environment variables abbreviated to the isolated root above):

```bash
env HOME="$ROOT/home" \
  XDG_CONFIG_HOME="$ROOT/config" \
  XDG_STATE_HOME="$ROOT/state" \
  XDG_RUNTIME_DIR="$ROOT/runtime" \
  XDG_CACHE_HOME="$ROOT/cache" \
  UV_TOOL_DIR="$ROOT/uv-tools" \
  UV_TOOL_BIN_DIR="$ROOT/uv-bin" \
  uv tool install --force \
    'git+https://github.com/bgconley/vela.git@de9b0a13f2ef7166014794bb211845dd3ba96123'
```

Result: exit 0. `uv 0.9.17` fetched the exact commit, built `vela`, and
reported `vela==0.1.0 (from git+https://github.com/bgconley/vela.git@de9b0a13...)`.
The installed module resolved to:

```text
/private/tmp/vela-quickstart-installed-de9b0a13f2ef/uv-tools/vela/lib/python3.11/site-packages/vela/__init__.py
```

This is a real tool installation, not an import from the active checkout.

## CLI checks

```text
$ vela --version
0.1.0
[exit 0]
```

`vela --help` exited 0 and rendered the top-level help, including the explicit
`tui`, `doctor`, `list`, `run`, `smoke`, `agent`, `build`, `config`, `deploy`,
`model`, `runs`, and `targets` commands.

## Pristine first-run PTY

The bare `vela` command was launched in a real 80x24 PTY from the empty
`$ROOT/work` directory with the isolated environment above. It started without
a registry-load exception and rendered these first-run states:

```text
Vela  ⊕lcl●  no config selected
○ IDLE
No configs yet — press n to create your first deployment · ? help
INFO Vela ready
INFO Quick start:
INFO   t  add or bootstrap a target (local works out of the box)
INFO   n  create a deployment — pin a model & build inside the wizard
INFO   ⏎  review · s saves & smoke-tests it
INFO   l  launch the saved config
```

Sending `q` exited the PTY command with exit 0. A follow-up `vela list` produced
the honest empty result:

```text
no configs found in: /Users/brennanconley/configs, /tmp/vela-quickstart-installed-de9b0a13f2ef/config/vela/configs — create one with 'vela deploy create' or the TUI (n)
```

`/Users/brennanconley/configs` did not exist. There were no YAML files under the
isolated config root and no files at all in the empty working directory, so the
installed distribution did not bundle or discover checkout configs.

## Cleanup proof

The first run correctly started a local agent in its isolated runtime. Cleanup
used the installed CLI against only that isolated socket:

```text
$ vela agent stop --socket /tmp/vela-quickstart-installed-de9b0a13f2ef/runtime/vela/agent.sock --json
{"pid": 27049, "status": "stopped", "version": "0.1.0", ...}
```

After cleanup:

```text
isolated agent process: absent
isolated agent socket:  absent
work/config/state/runtime files: none
```

The host's normal Vela runtime and daemon were not addressed by any command.

## Scope boundary

This certifies the published remediation branch at the exact SHA. The README's
literal unqualified install URL still resolves `main`; `origin/main` was
`88d18d897aabe87184a10575bcf8b52842ff20af` at verification time. Therefore the
literal installed-tool quickstart remains merge-dependent and is not claimed by
this supplemental branch proof.
