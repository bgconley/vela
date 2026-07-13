# Oxcart-local visible release validation

This is the release-proof runbook for the owner-directed topology where `oxcart`
is both the Vela controller and the GPU target. The controller uses Vela's
`local` socket transport on Oxcart; the browser reaches only a loopback
`textual-serve` listener through an SSH tunnel.

Blackbird is not contacted. Do not set `VELA_REMOTE_TARGET`, select Blackbird in
the UI, restart a shared daemon, or stop/replace any pre-existing container. All
source, Python, XDG state, Vela daemon state, configs, and temporary evidence in
this procedure live under one run-owned directory. The profile's durable run
records use its dedicated validation-only runs directory; the procedure copies
the two exact run-id families into evidence and then removes only those files.
The only container this run may create or clean is
`vela-oxcart-qwen36-27b-fp8-mtp-vl`, and cleanup requires both exact Vela
ownership labels.

## 1. Freeze the revision and evidence name

Start from a clean, reviewed, committed, and pushed tree on the Mac. Release
evidence is invalid for an uncommitted tree or a different remote branch head.

```bash
cd /path/to/vela
test -z "$(git status --porcelain=v1)"
export BRANCH="$(git branch --show-current)"
export SHA="$(git rev-parse HEAD)"
test "${#SHA}" -eq 40
git push origin "$BRANCH"
export RUN_ID="oxcart-${SHA%${SHA#????????????}}-$(date -u +%Y%m%dT%H%M%SZ)"
export LOCAL_EVIDENCE="$PWD/artifacts/human-workflow-validation/$(date -u +%F)/$RUN_ID"
mkdir -p "$LOCAL_EVIDENCE/screenshots"
printf 'branch=%s\nsha=%s\nrun_id=%s\n' "$BRANCH" "$SHA" "$RUN_ID" \
  > "$LOCAL_EVIDENCE/requested-revision.txt"
```

Use those exact `BRANCH`, `SHA`, and `RUN_ID` values in every following shell.
Do not substitute a short SHA where the runbook expects the full value.

## 2. Create an owned exact-SHA controller on Oxcart

Open an SSH shell to Oxcart and set the values copied from the Mac. The reusable
checkout is fetch authority only; it is never executed, cleaned, or reset.

```bash
set -euo pipefail
export BRANCH='<branch copied from Mac>'
export SHA='<full 40-character commit copied from Mac>'
export RUN_ID='<run id copied from Mac>'
export REPO=/home/bgconley/repos/lab-tui
export ROOT="/tank/work/validation/vela-oxcart-pilot-$RUN_ID"
export WORKTREE="$ROOT/source"
export VENV="$ROOT/venv"
export EVIDENCE="$ROOT/evidence"
export VELA_CONFIGS="$ROOT/configs"
export VELA_AGENT_RUNTIME_DIR="$ROOT/agent-runtime"
export XDG_CONFIG_HOME="$ROOT/xdg/config"
export XDG_DATA_HOME="$ROOT/xdg/data"
export XDG_STATE_HOME="$ROOT/xdg/state"
export HF_HOME=/tank/ai/models/qwen36-27b-fp8/hf-cache
export HF_HUB_CACHE=/tank/ai/models/qwen36-27b-fp8/hf-cache/hub
export UI_PORT=8815
export PROFILE=oxcart-qwen36-27b-fp8-mtp-vl
export VALIDATION_CONTAINER=vela-oxcart-qwen36-27b-fp8-mtp-vl
export RUNS_DIR=/tank/ai/models/qwen36-27b-fp8/vllm-rp6000-mtp-vl/vela-runs
unset HF_HUB_OFFLINE

test "$(hostname -s)" = oxcart
test "${#SHA}" -eq 40
mkdir -p "$ROOT" "$EVIDENCE" "$VELA_CONFIGS" "$VELA_AGENT_RUNTIME_DIR"
git -C "$REPO" status --porcelain=v1 -uno > "$EVIDENCE/reusable-status-before.txt"
git -C "$REPO" fetch --no-tags origin "$BRANCH"
test "$(git -C "$REPO" rev-parse FETCH_HEAD)" = "$SHA"
git -C "$REPO" worktree add --detach "$WORKTREE" "$SHA"
test "$(git -C "$WORKTREE" rev-parse HEAD)" = "$SHA"
git -C "$WORKTREE" status --porcelain=v1 > "$EVIDENCE/source-status-before.txt"
test ! -s "$EVIDENCE/source-status-before.txt"

python3 -m venv "$VENV"
"$VENV/bin/pip" install "$WORKTREE" textual-serve==1.1.3 Pillow
export PYTHONPATH="$WORKTREE/src"
export PATH="$VENV/bin:$PATH"
"$VENV/bin/python" -c 'import vela; print(vela.__file__)' \
  > "$EVIDENCE/vela-import-path.txt"
grep -F "$WORKTREE/src/vela/" "$EVIDENCE/vela-import-path.txt"
printf '%s\n' "$SHA" > "$EVIDENCE/source-sha.txt"
"$VENV/bin/pip" freeze > "$EVIDENCE/python-packages.txt"
```

`PYTHONPATH` deliberately makes the owned pristine worktree, not a reusable
editable checkout or a stale wheel, the code authority for the controller and
its child daemon.

## 3. Prove controller cache authority and take the baseline

The controller must scan the real Oxcart Hugging Face cache. Container mounts do
not establish controller-side pin/cache truth. Keep controller-side
`HF_HUB_OFFLINE` unset; the checked-in Docker profile independently keeps the
launched container offline.

```bash
"$VENV/bin/python" - <<'PY' | tee "$EVIDENCE/controller-hf-cache.json"
import json
import os
import huggingface_hub.constants

expected = "/tank/ai/models/qwen36-27b-fp8/hf-cache/hub"
actual = str(huggingface_hub.constants.HF_HUB_CACHE)
print(json.dumps({"HF_HOME": os.environ.get("HF_HOME"), "HF_HUB_CACHE": actual}))
if actual != expected:
    raise SystemExit(f"HF_HUB_CACHE mismatch: expected {expected}, got {actual}")
if os.environ.get("HF_HUB_OFFLINE"):
    raise SystemExit("controller-side HF_HUB_OFFLINE must be unset")
PY

cd "$WORKTREE"
"$VENV/bin/python" scripts/oxcart_live_guard.py preflight --snapshot \
  "$EVIDENCE/guard-baseline.json" | tee "$EVIDENCE/guard-preflight.json"

hostname -f > "$EVIDENCE/hostname-before.txt"
docker ps -a --no-trunc --format '{{json .}}' | sort \
  > "$EVIDENCE/containers-before.jsonl"
nvidia-smi --query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu \
  --format=csv,noheader > "$EVIDENCE/gpus-before.csv"
ss -ltn > "$EVIDENCE/listeners-before.txt"
ps -eo pid=,ppid=,lstart=,comm= > "$EVIDENCE/processes-before.txt"
"$VENV/bin/vela" agent status --json > "$EVIDENCE/owned-daemon-before.json"
```

Preflight must be green before opening the UI. It fails closed unless Oxcart is
idle enough for the proof, port `18004` is free, the pinned image and immutable
model snapshot exist, and the validation container is absent. Treat any
unrelated GPU/container change as a scheduling problem; do not repair it here.

Start only the run-owned local daemon and retain its identity:

```bash
"$VENV/bin/vela" agent start --json | tee "$EVIDENCE/owned-daemon-start-0.json"
"$VENV/bin/vela" agent status --json | tee "$EVIDENCE/owned-daemon-status-0.json"
```

## 4. Serve the real UI through loopback only

In a dedicated Oxcart shell with the same exported environment, run this in the
foreground. Stop it with `Ctrl+C` only after closing the browser tab. Repeat the
same command whenever the steps below say to cold-start the app.

```bash
"$VENV/bin/python" -c '
import os, shlex
from textual_serve.server import Server
command = shlex.join([
    os.path.join(os.environ["VENV"], "bin", "vela"),
    "--configs-dir", os.environ["VELA_CONFIGS"],
    "--target", "local",
])
Server(command, host="127.0.0.1", port=int(os.environ["UI_PORT"]),
       title="Vela Oxcart validation").serve()
' 2>&1 | tee "$EVIDENCE/textual-serve.log"
```

On the Mac, open a second terminal and keep this tunnel in the foreground:

```bash
ssh -N -L 127.0.0.1:8815:127.0.0.1:8815 bgconley@10.25.0.50
```

Open `http://127.0.0.1:8815` in the visible browser. Never bind the server or
tunnel to `0.0.0.0`; this UI can launch and stop compute.

## 5. Visible create, pin, review, and save-only journey

Drive the UI as a human and save screenshots under the Mac
`$LOCAL_EVIDENCE/screenshots` directory.

1. On the dashboard and New Deployment Target step, verify the active `local`
   target identifies host `oxcart`, not the Mac or Blackbird. Capture
   `01-local-is-oxcart`.
2. Select the `Oxcart Qwen3.6 27B FP8 MTP + vision` recipe. Inspect every field
   and its source/default guidance. The recipe must show local bind `127.0.0.1`,
   port `18004`, immutable Docker digest, validation-only container and runs
   directory, cache mounts, MTP/vision flags, and no eviction list. Capture
   `02-recipe-fields`.
3. Choose `Pin HF repo`, enter `Qwen/Qwen3.6-27B-FP8`, and enter full revision
   `e89b16ebf1988b3d6befa7de50abc2d76f26eb09`. Leave `Download now` off: this
   proof must use the existing exact snapshot. Submit, return once to the
   wizard, select the new pin, and verify `cached` plus the full resolved SHA.
   Capture `03-pin-full-sha-cache`.
4. Name the profile `oxcart-qwen36-27b-fp8-mtp-vl`. On Review, inspect target,
   required hostname, model repo/ref/full SHA, served id, image digest, redacted
   environment, mounts/caches, resolved argv, bind/exposure, and the explicit
   absence of eviction/destructive actions. Capture `04-review`.
5. Save without launching. Capture `05-saved-not-launched`; verify no validation
   container and no listener on `18004` appeared.

In the Oxcart control shell, retain the saved identity:

```bash
export PROFILE_PATH="$VELA_CONFIGS/$PROFILE.yaml"
test -f "$PROFILE_PATH"
sha256sum "$PROFILE_PATH" | tee "$EVIDENCE/profile-saved.sha256"
"$VENV/bin/vela" run "$PROFILE" --configs-dir "$VELA_CONFIGS" \
  --target local --preview > "$EVIDENCE/preview-saved.txt"
"$VENV/bin/python" - <<'PY'
import json, os
from pathlib import Path

root = Path(os.environ["EVIDENCE"])
lines = (root / "preview-saved.txt").read_text(encoding="utf-8").splitlines()
if len(lines) < 2 or not lines[0].startswith("cwd="):
    raise SystemExit("resolved preview has an unexpected shape")
(root / "resolved-command-environment.json").write_text(
    json.dumps(
        {"cwd": lines[0][4:], "environment": lines[1:-1], "argv": lines[-1]},
        indent=2,
        sort_keys=True,
    ) + "\n",
    encoding="utf-8",
)
PY
sha256sum "$EVIDENCE/resolved-command-environment.json" \
  | tee "$EVIDENCE/resolved-command-environment.sha256"
"$VENV/bin/vela" model list --target local --pinned-only --json \
  > "$EVIDENCE/model-registry-saved.json"
if docker inspect "$VALIDATION_CONTAINER" > "$EVIDENCE/save-only-container.json" 2>&1; then
  echo "save-only unexpectedly created $VALIDATION_CONTAINER" >&2
  exit 1
fi
```

## 6. Cold restart, launch, probe, Stop, and repeat

Close the browser tab and stop `textual-serve`. Then cold-restart only the owned
daemon. Do not run `vela agent restart` outside this isolated environment.

```bash
"$VENV/bin/vela" agent stop --json | tee "$EVIDENCE/owned-daemon-stop-1.json"
"$VENV/bin/vela" agent status --json > "$EVIDENCE/owned-daemon-down-1.json"
"$VENV/bin/vela" agent start --json | tee "$EVIDENCE/owned-daemon-start-1.json"
"$VENV/bin/vela" agent status --json > "$EVIDENCE/owned-daemon-status-1.json"
sha256sum "$PROFILE_PATH" > "$EVIDENCE/profile-run1.sha256"
"$VENV/bin/vela" run "$PROFILE" --configs-dir "$VELA_CONFIGS" \
  --target local --preview > "$EVIDENCE/preview-run1.txt"
cmp "$EVIDENCE/profile-saved.sha256" "$EVIDENCE/profile-run1.sha256"
cmp "$EVIDENCE/preview-saved.txt" "$EVIDENCE/preview-run1.txt"
```

Cold-start `textual-serve`, reconnect the browser, select the saved profile, and
capture `06-cold-reload-selected`. Launch through the UI. At READY, capture the
target/profile/model identity and complete phase timeline as `07-run1-ready`.

While READY, capture the run id, runtime identity, and live probes:

```bash
"$VENV/bin/vela" runs list --target local --json > "$EVIDENCE/runs-run1.json"
export RUN1_ID="$("$VENV/bin/python" - <<'PY'
import json, os
rows = json.load(open(os.path.join(os.environ["EVIDENCE"], "runs-run1.json")))["runs"]
matches = [row["run_id"] for row in rows if row.get("config") == os.environ["PROFILE"]]
if len(matches) != 1:
    raise SystemExit(f"expected one active profile run, got {matches}")
print(matches[0])
PY
)"
printf '%s\n' "$RUN1_ID" > "$EVIDENCE/run1-id.txt"
docker inspect "$VALIDATION_CONTAINER" > "$EVIDENCE/docker-run1.json"
nvidia-smi > "$EVIDENCE/nvidia-smi-run1.txt"
```

Run the following fixed endpoint probe with `RUN_LABEL=run1`, then repeat it as
`run2` during the second READY state. It requires exact `/v1/models`, an exact
temperature-zero text token, and actual left/right color understanding from a
real PNG data URL. The protected endpoints use the profile's fixed `EMPTY`
sentinel bearer credential; every request and response is retained.

```bash
export RUN_LABEL=run1
"$VENV/bin/python" - <<'PY'
import base64, io, json, os
from pathlib import Path

import httpx
from PIL import Image

root = Path(os.environ["EVIDENCE"])
label = os.environ["RUN_LABEL"]
served = "qwen36-27b-fp8-oxcart"
base = "http://127.0.0.1:18004"

def write(name, value):
    (root / f"{label}-{name}.json").write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

with httpx.Client(timeout=180.0) as client:
    auth = {"Authorization": "Bearer EMPTY"}
    health = client.get(base + "/health")
    health.raise_for_status()
    write("health", {"status_code": health.status_code, "body": health.text})

    models = client.get(base + "/v1/models", headers=auth)
    models.raise_for_status()
    models_body = models.json()
    ids = sorted(item["id"] for item in models_body.get("data", []))
    if ids != [served]:
        raise SystemExit(f"unexpected served model ids: {ids}")
    write("models", models_body)

    common = {
        "model": served,
        "temperature": 0,
        "seed": 7,
        "max_tokens": 32,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    text_request = {
        **common,
        "messages": [{"role": "user", "content":
                      "Reply with exactly VELA_TEXT_OK and nothing else."}],
    }
    text_response = client.post(
        base + "/v1/chat/completions", json=text_request, headers=auth
    )
    text_response.raise_for_status()
    text_body = text_response.json()
    if text_body["choices"][0]["message"]["content"].strip() != "VELA_TEXT_OK":
        raise SystemExit("deterministic text probe returned the wrong token")
    write("text-request", text_request)
    write("text-response", text_body)

    image = Image.new("RGB", (64, 32))
    image.paste((255, 0, 0), (0, 0, 32, 32))
    image.paste((0, 255, 0), (32, 0, 64, 32))
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(encoded.getvalue()).decode()
    vision_request = {
        **common,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text":
             "Identify the two color halves using exactly LEFT=RED; RIGHT=GREEN."},
        ]}],
    }
    vision_response = client.post(
        base + "/v1/chat/completions", json=vision_request, headers=auth
    )
    vision_response.raise_for_status()
    vision_body = vision_response.json()
    vision_text = str(vision_body["choices"][0]["message"]["content"]).lower()
    if not all(token in vision_text for token in ("left", "red", "right", "green")):
        raise SystemExit("data-URL vision probe did not identify both color halves")
    write("vision-request", vision_request)
    write("vision-response", vision_body)
PY
```

Stop through the UI and capture the unmistakable closure as `08-run1-stopped`.
Verify the container and port are gone. Then retain and validate the scrubbed run
artifact:

```bash
if docker inspect "$VALIDATION_CONTAINER" > /dev/null 2>&1; then exit 1; fi
if ss -ltn | grep -q ':18004 '; then exit 1; fi
"$VENV/bin/python" scripts/backend_evidence_check.py "$PROFILE" "$RUN1_ID" \
  --target local --timeout 120 | tee "$EVIDENCE/backend-run1.txt"
export ARTIFACT_RUN_ID="$RUN1_ID" ARTIFACT_OUT="$EVIDENCE/artifact-run1.json"
"$VENV/bin/python" - <<'PY'
import asyncio, json, os
from pathlib import Path
from vela.config.targets import load_targets_file
from vela.transport.factory import target_client_for_config

async def main():
    client = target_client_for_config(load_targets_file().by_name("local"))
    await client.connect()
    try:
        result = await client.call("read_run_artifact", {
            "run_id": os.environ["ARTIFACT_RUN_ID"],
            "config_name": os.environ["PROFILE"],
        })
    finally:
        await client.disconnect()
    Path(os.environ["ARTIFACT_OUT"]).write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
asyncio.run(main())
PY
```

Close the app/server, cold-restart the owned daemon again, recompute the hash and
preview, and require equality:

```bash
"$VENV/bin/vela" agent stop --json > "$EVIDENCE/owned-daemon-stop-2.json"
"$VENV/bin/vela" agent start --json > "$EVIDENCE/owned-daemon-start-2.json"
"$VENV/bin/vela" agent status --json > "$EVIDENCE/owned-daemon-status-2.json"
sha256sum "$PROFILE_PATH" > "$EVIDENCE/profile-run2.sha256"
"$VENV/bin/vela" run "$PROFILE" --configs-dir "$VELA_CONFIGS" \
  --target local --preview > "$EVIDENCE/preview-run2.txt"
cmp "$EVIDENCE/profile-saved.sha256" "$EVIDENCE/profile-run2.sha256"
cmp "$EVIDENCE/preview-saved.txt" "$EVIDENCE/preview-run2.txt"
```

Cold-start the app, reselect the same profile, launch again, and capture
`09-run2-ready`. Set `RUN_LABEL=run2`, run the endpoint probe again, capture
`RUN2_ID` using the same `runs list` procedure, then Stop in the UI and capture
`10-run2-stopped`. Run `backend_evidence_check.py` and the artifact-capture block
again with `RUN2_ID`, writing `backend-run2.txt`, `run2-id.txt`, and
`artifact-run2.json`.

Compare the immutable execution identity while deliberately excluding the
per-run container id:

```bash
"$VENV/bin/python" - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ["EVIDENCE"])
a = json.loads((root / "artifact-run1.json").read_text())
b = json.loads((root / "artifact-run2.json").read_text())
if a["config"] != b["config"]:
    raise SystemExit("normalized run config changed across cold restart")
keys = (
    "config_name", "model_ref", "model_entry_id", "model_repo_id",
    "model_revision", "model_commit_sha", "served_model_names", "runtime",
    "docker_container_name", "docker_image_digest",
)
left = {key: a["identity"].get(key) for key in keys}
right = {key: b["identity"].get(key) for key in keys}
if left != right:
    raise SystemExit(f"immutable launch identity changed: {left!r} != {right!r}")
(root / "identity-comparison.json").write_text(
    json.dumps({"equal": True, "run1": left, "run2": right}, indent=2,
               sort_keys=True) + "\n", encoding="utf-8"
)
PY
```

Both runs are stopped and their backend/sidecar projections are now retained.
Copy and checksum only their exact run-id artifact families, then remove those
known files from the dedicated validation runs directory. No wildcard broader
than either full run id is permitted:

```bash
export RUN1_ID RUN2_ID RUNS_DIR EVIDENCE
"$VENV/bin/python" - <<'PY'
import hashlib, json, os, shutil
from pathlib import Path

source = Path(os.environ["RUNS_DIR"])
evidence = Path(os.environ["EVIDENCE"]) / "run-artifacts"
index = {}
copied = []
for run_id in (os.environ["RUN1_ID"], os.environ["RUN2_ID"]):
    files = sorted(path for path in source.glob(run_id + "*") if path.is_file())
    if not files:
        raise SystemExit(f"no durable artifacts found for {run_id}")
    destination = evidence / run_id
    destination.mkdir(parents=True, exist_ok=False)
    rows = []
    for path in files:
        target = destination / path.name
        shutil.copy2(path, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        rows.append({"name": path.name, "sha256": digest})
        copied.append(path)
    index[run_id] = rows
for path in copied:
    path.unlink()
(Path(os.environ["EVIDENCE"]) / "run-artifacts-index.json").write_text(
    json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
```

## 7. Visible wrong-host failure before any container action

After Run 2 is stopped and `18004` is free, derive an isolated negative profile.
It keeps the owned container identity but requires a hostname that cannot match:

```bash
export WRONG_PROFILE=oxcart-wrong-host-proof
"$VENV/bin/python" - <<'PY'
import os
from pathlib import Path
import yaml
source = Path(os.environ["PROFILE_PATH"])
payload = yaml.safe_load(source.read_text(encoding="utf-8"))
payload["name"] = os.environ["WRONG_PROFILE"]
payload["launch"]["required_hostname"] = "definitely-not-oxcart"
destination = Path(os.environ["VELA_CONFIGS"]) / (os.environ["WRONG_PROFILE"] + ".yaml")
destination.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
PY
if docker inspect "$VALIDATION_CONTAINER" > /dev/null 2>&1; then exit 1; fi
```

Cold-start only the app so it discovers the file. Select the wrong-host profile
and press Launch. It must show a hostname preflight error while the app remains
responsive; open and close Help to prove responsiveness. Capture
`11-wrong-host-failed` and `12-after-failure-responsive`. Then prove no container
or port was created:

```bash
if docker inspect "$VALIDATION_CONTAINER" \
  > "$EVIDENCE/wrong-host-container-after.txt" 2>&1; then
  echo "wrong-host preflight created a container" >&2
  exit 1
fi
if ss -ltn | grep -q ':18004 '; then
  echo "wrong-host preflight opened port 18004" >&2
  exit 1
fi
printf 'wrong-host rejected before container and port creation\n' \
  > "$EVIDENCE/wrong-host-result.txt"
rm "$VELA_CONFIGS/$WRONG_PROFILE.yaml"
```

## 8. Stop, reconcile, and copy evidence

Close the browser and `textual-serve`, stop only the isolated daemon, and take
the final inventory:

```bash
"$VENV/bin/vela" agent stop --json | tee "$EVIDENCE/owned-daemon-stop-final.json"
"$VENV/bin/vela" agent status --json > "$EVIDENCE/owned-daemon-after.json"
docker ps -a --no-trunc --format '{{json .}}' | sort \
  > "$EVIDENCE/containers-after.jsonl"
nvidia-smi --query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu \
  --format=csv,noheader > "$EVIDENCE/gpus-after.csv"
ss -ltn > "$EVIDENCE/listeners-after.txt"
ps -eo pid=,ppid=,lstart=,comm= > "$EVIDENCE/processes-after.txt"
git -C "$WORKTREE" status --porcelain=v1 > "$EVIDENCE/source-status-after.txt"
test ! -s "$EVIDENCE/source-status-after.txt"
git -C "$REPO" status --porcelain=v1 -uno > "$EVIDENCE/reusable-status-after.txt"
cmp "$EVIDENCE/reusable-status-before.txt" "$EVIDENCE/reusable-status-after.txt"

cd "$WORKTREE"
"$VENV/bin/python" scripts/oxcart_live_guard.py postflight --snapshot \
  "$EVIDENCE/guard-baseline.json" | tee "$EVIDENCE/guard-postflight.json"
```

Postflight must pass without cleanup on the normal path. If and only if its sole
problem is residue from the exact validation container, use the bounded cleanup
below, then rerun postflight. Do not use it for unrelated-container, GPU, port,
or hostname failures; stop and investigate instead. The script rechecks the
exact name, container id, host, and both ownership labels immediately before
stop/removal.

```bash
"$VENV/bin/python" scripts/oxcart_live_guard.py cleanup \
  | tee "$EVIDENCE/guard-cleanup.json"
"$VENV/bin/python" scripts/oxcart_live_guard.py postflight --snapshot \
  "$EVIDENCE/guard-baseline.json" | tee "$EVIDENCE/guard-postflight-after-cleanup.json"
```

From the Mac, copy the host evidence beside the visible screenshots:

```bash
scp -r \
  "bgconley@10.25.0.50:/tank/work/validation/vela-oxcart-pilot-$RUN_ID/evidence" \
  "$LOCAL_EVIDENCE/host"
```

Review every file for secrets before retention. `EMPTY` is the fixed validation
API key sentinel used only by this isolated profile; never add `HF_TOKEN`, shell
environment dumps, target credentials, or real secrets to screenshots or
evidence.

Create the manifest only after both launches, both backend checks, the negative
case, cleanup proof, and screenshots are present:

```bash
export BRANCH SHA RUN_ID LOCAL_EVIDENCE
python3 - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ["LOCAL_EVIDENCE"])
required = [
    "host/guard-preflight.json", "host/guard-postflight.json",
    "host/profile-saved.sha256", "host/profile-run1.sha256",
    "host/profile-run2.sha256", "host/preview-saved.txt",
    "host/preview-run1.txt", "host/preview-run2.txt",
    "host/backend-run1.txt", "host/backend-run2.txt",
    "host/artifact-run1.json", "host/artifact-run2.json",
    "host/identity-comparison.json", "host/wrong-host-result.txt",
    "host/resolved-command-environment.json",
    "host/resolved-command-environment.sha256",
    "host/run-artifacts-index.json",
]
missing = [name for name in required if not (root / name).is_file()]
screens = sorted(str(path.relative_to(root)) for path in (root / "screenshots").iterdir()
                 if path.is_file())
if missing or len(screens) < 12:
    raise SystemExit(f"incomplete evidence: missing={missing}, screenshots={len(screens)}")
manifest = {
    "schema": "vela.oxcart-local-validation.v1",
    "branch": os.environ["BRANCH"],
    "sha": os.environ["SHA"],
    "run_id": os.environ["RUN_ID"],
    "controller": "oxcart",
    "target": "local@oxcart",
    "profile": "oxcart-qwen36-27b-fp8-mtp-vl",
    "model_revision": "e89b16ebf1988b3d6befa7de50abc2d76f26eb09",
    "served_model_id": "qwen36-27b-fp8-oxcart",
    "resolved_command_environment_sha256":
        (root / "host/resolved-command-environment.sha256")
        .read_text(encoding="utf-8").split()[0],
    "launches": 2,
    "cold_restarts": 2,
    "wrong_hostname_fail_before_container": "pass",
    "cleanup_and_unrelated_state": "pass",
    "blackbird_touched": False,
    "shared_daemon_touched": False,
    "screenshots": screens,
    "required_files": required,
}
(root / "manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

cd "$LOCAL_EVIDENCE"
find . -type f ! -name checksums.sha256 -print0 | sort -z \
  | xargs -0 shasum -a 256 > checksums.sha256
shasum -a 256 -c checksums.sha256
```

After the checksums verify locally, remove only the run-owned remote resources:

```bash
git -C /home/bgconley/repos/lab-tui worktree remove --force \
  "/tank/work/validation/vela-oxcart-pilot-$RUN_ID/source"
rm -rf "/tank/work/validation/vela-oxcart-pilot-$RUN_ID"
```

Do not claim release proof until the retained manifest names the final commit,
the exact-SHA source worktree stayed clean, the two config hashes and previews
match, both run artifacts have equal immutable identities, endpoint/backend
checks pass twice, the wrong-host case creates nothing, the guard postflight is
green, and the screenshots show the complete human journey.
