# Hermetic cloned-repo quickstart

- Verdict: PASS (supplemental branch certification)
- Certified source revision: `de9b0a13f2ef7166014794bb211845dd3ba96123`
- Published branch: `remediate/2026-07-09-review`
- Executed (UTC): `2026-07-13T07:53:39Z` through `2026-07-13T07:55:02Z`
- Clean clone: `/tmp/vela-quickstart-clone-de9b0a13f2ef/repo`
- External virtualenv: `/tmp/vela-quickstart-clone-de9b0a13f2ef/venv`
- Isolation: dedicated `HOME` and XDG config/state/runtime/cache roots.

## Published revision proof

```bash
git clone --branch remediate/2026-07-09-review --single-branch \
  https://github.com/bgconley/vela.git "$ROOT/repo"
git -C "$ROOT/repo" checkout --detach \
  de9b0a13f2ef7166014794bb211845dd3ba96123
```

```text
HEAD=de9b0a13f2ef7166014794bb211845dd3ba96123
origin/remediate/2026-07-09-review=de9b0a13f2ef7166014794bb211845dd3ba96123
```

The clean clone was detached at the certified SHA before installation or
execution.

## README installation route

From the clean clone's repository root, using a fresh Python 3.11 virtualenv:

```bash
pip install -e ".[dev]"
```

Result: exit 0; pip built the editable `vela-0.1.0` wheel and installed Vela plus
the declared developer dependencies. The installed module resolved to the clone,
not the active workspace:

```text
/private/tmp/vela-quickstart-clone-de9b0a13f2ef/repo/src/vela/__init__.py
```

## README no-GPU demo, verbatim

All three documented commands were run from the clean clone root with `vela`
resolved from the fresh virtualenv.

```text
$ vela list
fake-child fake/model
qwen3-32b-fp8-62001 /tank/trt/models/Qwen3-32B-FP8
qwen36-27b-bf16-rp6000-blackbird Qwen/Qwen3.6-27B
qwen36-27b-fp8-kvfp8-rp6000-blackbird Qwen/Qwen3.6-27B-FP8
real-vllm-example mistralai/Mistral-7B-Instruct-v0.3
tiny-random-llama-detached-blackbird hf-internal-testing/tiny-random-LlamaForCausalLM
[exit 0]
```

The original output is tab-delimited; spaces are used above for readability.
Most importantly, the bundled `./configs/fake-child.yaml` was discovered.

```text
$ vela run fake-child --preview
cwd=/private/tmp/vela-quickstart-clone-de9b0a13f2ef/repo
PYTHONUNBUFFERED=1
./scripts/fake_vllm_child.py serve fake/model --served-model-name fake-model --host 127.0.0.1 --port 8765
[exit 0]
```

The preview printed the resolved launch and did not launch the child.

```text
$ vela smoke fake-child
READY http://127.0.0.1:8765 models=fake-model
[exit 0]
```

The real fake-child process reached its HTTP readiness/model gate, emitted
`READY`, and the smoke command stopped it before returning.

## Closure and residue proof

Immediately after smoke:

```text
$ vela runs list --json
{"runs": []}

$ lsof -nP -iTCP:8765 -sTCP:LISTEN
[no output; exit 1]

$ ps -ax -o command= | rg -q '[f]ake_vllm_child.py'
[no output; exit 1]
```

The command-created local agent was on the isolated socket and reported the
certified revision before cleanup:

```text
status=running
pid=30290
revision=v0.1.0-108-gde9b0a1
socket=/tmp/vela-quickstart-clone-de9b0a13f2ef/runtime/vela/agent.sock
```

It was stopped through that explicit socket only. Final checks:

```text
TCP 8765 listener: absent (lsof exit 1)
fake_vllm_child.py process: absent (rg exit 1)
isolated agent process: absent
isolated agent socket: absent
git status --short: empty
git HEAD: de9b0a13f2ef7166014794bb211845dd3ba96123
```

The host's normal Vela runtime and daemon were not addressed by any command.

## Scope boundary

This proves the cloned-repo workflow for the published remediation branch at its
exact SHA. The README's literal unqualified `git clone` command still checks out
`main`, which was `88d18d897aabe87184a10575bcf8b52842ff20af` at verification
time. The literal main-branch quickstart therefore remains merge-dependent.
