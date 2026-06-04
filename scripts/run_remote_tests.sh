#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/run_remote_tests.sh USER@GPU_HOST /absolute/remote/path [real-config-name]

Runs the GPU-box validation flow after local commit/push; the GPU node pulls from git.

The default flow is safe on machines without vLLM/GPU access:
  - install editable dev package
  - run the no-GPU pytest suite
  - run CLI preview/list checks
  - print host GPU/vLLM profile diagnostics

If real-config-name is provided, the remote host also runs:
  - vllm-loader preview REAL_CONFIG
  - timeout-bound vllm-loader smoke-tui REAL_CONFIG

Optional real artifact validation is enabled by local environment variables:
  - VLLM_LOADER_REMOTE_PYTEST_ARGS overrides the remote pytest args (default: -q)
  - VLLM_LOADER_REMOTE_TARGET runs build/model/config checks through a named target
  - VLLM_LOADER_REMOTE_BUILD_SPEC='vllm==X.Y.Z' runs build add/verify
  - VLLM_LOADER_REMOTE_BUILD_METHOD defaults to pip
  - VLLM_LOADER_REMOTE_BUILD_LABEL defaults to remote-smoke-build
  - VLLM_LOADER_REMOTE_MODEL_REPO pins a HF repo before download
  - VLLM_LOADER_REMOTE_MODEL_ID defaults to remote-smoke-model
  - VLLM_LOADER_REMOTE_MODEL_REF downloads an existing pinned entry instead
  - VLLM_LOADER_REMOTE_MODEL_REVISION optionally pins/downloads a revision
  - VLLM_LOADER_REMOTE_REAL_RESUME_CONFIG runs real-model resume/restart validation
  - VLLM_LOADER_REMOTE_ARTIFACT=1 writes a dated Markdown validation record
  - VLLM_LOADER_REMOTE_ARTIFACT_DIR overrides artifacts/remote-validation
  - VLLM_LOADER_REMOTE_ARTIFACT_NAME writes a deterministic artifact filename

Example:
  scripts/run_remote_tests.sh blackbird /srv/lab-tui
  scripts/run_remote_tests.sh blackbird /srv/lab-tui llama-3.1-8b
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 2 || $# -gt 3 ]]; then
  usage
  exit $([[ $# -eq 1 ]] && [[ "${1:-}" =~ ^- ]] && echo 0 || echo 2)
fi

host="$1"
remote_path="$2"
real_config="${3:-}"
remote_timeout="${VLLM_LOADER_REMOTE_TIMEOUT:-1800}"
remote_python="${VLLM_LOADER_REMOTE_PYTHON:-auto}"
remote_venv="${VLLM_LOADER_REMOTE_VENV:-/tank/venvs/lab-tui}"
remote_build_method="${VLLM_LOADER_REMOTE_BUILD_METHOD:-pip}"
remote_build_spec="${VLLM_LOADER_REMOTE_BUILD_SPEC:-}"
remote_build_label="${VLLM_LOADER_REMOTE_BUILD_LABEL:-remote-smoke-build}"
remote_model_id="${VLLM_LOADER_REMOTE_MODEL_ID:-remote-smoke-model}"
remote_model_repo="${VLLM_LOADER_REMOTE_MODEL_REPO:-}"
remote_model_ref="${VLLM_LOADER_REMOTE_MODEL_REF:-}"
remote_model_revision="${VLLM_LOADER_REMOTE_MODEL_REVISION:-}"
remote_target="${VLLM_LOADER_REMOTE_TARGET:-}"
remote_pytest_args="${VLLM_LOADER_REMOTE_PYTEST_ARGS:-}"
remote_real_resume_config="${VLLM_LOADER_REMOTE_REAL_RESUME_CONFIG:-}"
artifact_enabled="${VLLM_LOADER_REMOTE_ARTIFACT:-}"
artifact_dir="${VLLM_LOADER_REMOTE_ARTIFACT_DIR:-}"
artifact_name="${VLLM_LOADER_REMOTE_ARTIFACT_NAME:-}"
empty_arg="__VLLM_LOADER_EMPTY__"
append_remote_arg() {
  if [[ -n "$1" ]]; then
    ssh_cmd+=("$1")
  else
    ssh_cmd+=("$empty_arg")
  fi
}
quote_remote_word() {
  printf "%q" "$1"
}
ssh_cmd=(ssh)
if [[ -n "${VLLM_LOADER_SSH_OPTS:-}" ]]; then
  # shellcheck disable=SC2206
  ssh_cmd+=(${VLLM_LOADER_SSH_OPTS})
fi
ssh_cmd+=("$host")
remote_env=()
if [[ -n "$remote_target" ]]; then
  remote_env+=("$(quote_remote_word "VLLM_LOADER_REMOTE_TARGET=$remote_target")")
fi
if [[ -n "$remote_pytest_args" ]]; then
  remote_env+=("$(quote_remote_word "VLLM_LOADER_REMOTE_PYTEST_ARGS=$remote_pytest_args")")
fi
if [[ -n "$remote_real_resume_config" ]]; then
  remote_env+=("$(quote_remote_word "VLLM_LOADER_REMOTE_REAL_RESUME_CONFIG=$remote_real_resume_config")")
fi
if [[ ${#remote_env[@]} -gt 0 ]]; then
  ssh_cmd+=(env "${remote_env[@]}")
fi
ssh_cmd+=(bash -s -- "$remote_path" "$remote_timeout" "$remote_python" "$remote_venv")
if [[ -n "$real_config" ]]; then
  ssh_cmd+=("$real_config")
fi
if [[ -n "$remote_build_spec" || -n "$remote_model_repo" || -n "$remote_model_ref" ]]; then
  if [[ -z "$real_config" ]]; then
    append_remote_arg ""
  fi
  append_remote_arg "$remote_build_method"
  append_remote_arg "$remote_build_spec"
  append_remote_arg "$remote_build_label"
  append_remote_arg "$remote_model_id"
  append_remote_arg "$remote_model_repo"
  append_remote_arg "$remote_model_ref"
  append_remote_arg "$remote_model_revision"
fi

if [[ -z "$artifact_enabled" && -n "$artifact_dir" ]]; then
  artifact_enabled=1
fi
if [[ "$artifact_enabled" == "1" && -z "$artifact_dir" ]]; then
  artifact_dir="artifacts/remote-validation"
fi

_validation_slug() {
  local value="$1"
  local slug
  slug="$(printf '%s' "$value" | tr -cs '[:alnum:]._-' '-' | sed -e 's/^-//' -e 's/-$//')"
  if [[ -z "$slug" ]]; then
    slug="validation"
  fi
  printf '%s' "$slug"
}

run_remote_validation() {
  "${ssh_cmd[@]}" <<'REMOTE'
set -euo pipefail

empty_arg="__VLLM_LOADER_EMPTY__"
_remote_arg_or_empty() {
  if [[ "${1:-}" == "$empty_arg" ]]; then
    printf ''
  else
    printf '%s' "${1:-}"
  fi
}

remote_path="$1"
remote_timeout="$2"
remote_python="${3:-auto}"
remote_venv="${4:-/tank/venvs/lab-tui}"
real_config="$(_remote_arg_or_empty "${5:-}")"
remote_build_method="$(_remote_arg_or_empty "${6:-pip}")"
remote_build_spec="$(_remote_arg_or_empty "${7:-}")"
remote_build_label="$(_remote_arg_or_empty "${8:-remote-smoke-build}")"
remote_model_id="$(_remote_arg_or_empty "${9:-remote-smoke-model}")"
remote_model_repo="$(_remote_arg_or_empty "${10:-}")"
remote_model_ref="$(_remote_arg_or_empty "${11:-}")"
remote_model_revision="$(_remote_arg_or_empty "${12:-}")"
remote_target="${VLLM_LOADER_REMOTE_TARGET:-}"
remote_pytest_args="${VLLM_LOADER_REMOTE_PYTEST_ARGS:--q}"
remote_real_resume_config="${VLLM_LOADER_REMOTE_REAL_RESUME_CONFIG:-}"
read -r -a pytest_args <<< "$remote_pytest_args"
target_args=()
if [[ -n "$remote_target" ]]; then
  target_args=(--target "$remote_target")
fi
if [[ -z "$remote_build_method" ]]; then
  remote_build_method="pip"
fi
if [[ -z "$remote_build_label" ]]; then
  remote_build_label="remote-smoke-build"
fi
if [[ -z "$remote_model_id" ]]; then
  remote_model_id="remote-smoke-model"
fi

if [[ "$remote_python" == "auto" ]]; then
  if [[ -x /tank/preproc/venv/bin/python ]]; then
    remote_python=/tank/preproc/venv/bin/python
  elif command -v python3 >/dev/null 2>&1; then
    remote_python=python3
  elif command -v python >/dev/null 2>&1; then
    remote_python=python
  else
    echo "python3 or python is required on the remote host" >&2
    exit 127
  fi
fi

cd "$remote_path"
echo "== Remote git pull =="
git -C "$remote_path" rev-parse --is-inside-work-tree >/dev/null
git -C "$remote_path" pull --ff-only origin main
venv_python="$remote_venv/bin/python"
venv_bin="$remote_venv/bin"
if [[ ! -x "$venv_python" ]]; then
  mkdir -p "$(dirname "$remote_venv")"
  "$remote_python" -m venv "$remote_venv"
fi
if [[ ! -x "$venv_python" ]]; then
  echo "Remote venv was not created at $remote_venv; install python3-venv/ensurepip or set VLLM_LOADER_REMOTE_PYTHON to a venv-capable Python." >&2
  exit 127
fi
if ! "$venv_python" -m pip --version >/dev/null 2>&1; then
  echo "Remote venv lacks pip; install python3-venv/ensurepip or set VLLM_LOADER_REMOTE_PYTHON to a venv-capable Python: $remote_python" >&2
  exit 127
fi
export PATH="$venv_bin:$PATH"
"$venv_python" -m pip install -e ".[dev]"
echo "== Remote agent restart =="
"$venv_bin/vllm-loader" agent restart
echo "== Remote host =="
hostname
"$venv_python" - <<'PY'
from vllm_loader.monitoring.gpu import sample_gpus

result = sample_gpus()
print(f"GPU unavailable={result.unavailable} note={result.note}")
for sample in result.samples:
    mig = f" mig={sample.mig_instance_id}" if sample.mig_instance_id else ""
    util = "unknown" if sample.utilization_percent is None else f"{sample.utilization_percent}%"
    print(
        "GPU "
        f"{sample.visible_index} {sample.name} {sample.uuid}{mig} "
        f"mem={sample.memory_used_mb}/{sample.memory_total_mb}MiB util={util}"
    )
PY
if command -v vllm >/dev/null 2>&1; then
  echo "== vLLM version =="
  vllm --version || true
  echo "== vLLM serve help flags =="
  vllm serve --help | sed -n '1,80p' || true
else
  echo "vllm not found on PATH; no-GPU package checks will still run"
fi
"$venv_python" -m ruff check .
"$venv_python" -m pytest "${pytest_args[@]}"
"$venv_bin/vllm-loader" list
"$venv_bin/vllm-loader" preview fake-child
echo "== Daemon restart live-run survival =="
"$venv_python" - "$remote_path" "$venv_bin" <<'PY'
import asyncio
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from vllm_loader.transport.subprocess import SubprocessTargetClient


remote_path = Path(sys.argv[1])
venv_bin = Path(sys.argv[2])
config_name = "daemon-restart-fake"
tmp_root = Path(tempfile.mkdtemp(prefix="vllm-loader-daemon-restart-"))
runs_dir = tmp_root / "runs"
config_path = tmp_root / f"{config_name}.yaml"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _agent_client() -> SubprocessTargetClient:
    return SubprocessTargetClient(
        [str(venv_bin / "vllm-loader"), "agent", "connect"],
        cwd=remote_path,
    )


async def _wait_ready(client: SubprocessTargetClient, run_id: str) -> dict:
    for _ in range(60):
        health = await client.call("health", {"run_id": run_id})
        if health.get("ready"):
            return health
        await asyncio.sleep(0.5)
    raise RuntimeError(f"run did not become ready after daemon restart test: {run_id}")


async def _main() -> None:
    port = _free_port()
    config_path.write_text(
        f"""
name: {config_name}
model: fake/model
served_model_name: fake-model
command:
  entrypoint: serve
  executable: {remote_path / "scripts" / "fake_vllm_child.py"}
server:
  host: 127.0.0.1
  port: {port}
logging:
  request_logging: false
launch:
  mode: detached
  runs_dir: {runs_dir}
  ready_timeout_seconds: 30
""".lstrip(),
        encoding="utf-8",
    )

    run_id = f"daemon-restart-{uuid.uuid4().hex}"
    client = _agent_client()
    await client.connect()
    try:
        await client.call(
            "launch",
            {
                "run_id": run_id,
                "name": config_name,
                "configs_dir": str(tmp_root),
            },
        )
        await _wait_ready(client, run_id)
    finally:
        await client.disconnect()

    subprocess.run([str(venv_bin / "vllm-loader"), "agent", "restart"], check=True)

    client = _agent_client()
    await client.connect()
    try:
        discovered = await client.call("discover_runs", {"runs_dirs": [str(runs_dir)]})
        discovered_ids = {
            str(run.get("run_id"))
            for run in discovered.get("runs", [])
            if isinstance(run, dict)
        }
        if run_id not in discovered_ids:
            raise RuntimeError(
                f"run {run_id} not rediscovered after daemon restart: {discovered}"
            )
        reattached = await client.call("reattach", {"run_id": run_id})
        if str(reattached.get("run_id")) != run_id:
            raise RuntimeError(f"reattach returned wrong run: {reattached}")
        health = await _wait_ready(client, run_id)
        await client.call(
            "stop",
            {
                "run_id": run_id,
                "interrupt_timeout": 2,
                "terminate_timeout": 2,
            },
        )
        waited = await client.call("wait", {"run_id": run_id})
    finally:
        await client.disconnect()

    shutil.rmtree(tmp_root, ignore_errors=True)
    print(
        "DAEMON_RESTART_LIVE_RUN_OK "
        f"run_id={run_id} port={port} "
        f"url={health.get('reachable_url')} "
        f"returncode={waited.get('returncode')}"
    )


asyncio.run(_main())
PY

echo "== Disconnect/reconnect stream resume =="
"$venv_python" - "$remote_path" "$venv_bin" <<'PY'
import asyncio
import contextlib
import shutil
import socket
import sys
import tempfile
import uuid
from pathlib import Path

from vllm_loader.transport.subprocess import SubprocessTargetClient


remote_path = Path(sys.argv[1])
venv_bin = Path(sys.argv[2])
config_name = "disconnect-reconnect-fake"
tmp_root = Path(tempfile.mkdtemp(prefix="vllm-loader-disconnect-reconnect-"))
runs_dir = tmp_root / "runs"
config_path = tmp_root / f"{config_name}.yaml"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _agent_client() -> SubprocessTargetClient:
    return SubprocessTargetClient(
        [str(venv_bin / "vllm-loader"), "agent", "connect"],
        cwd=remote_path,
    )


async def _wait_ready(client: SubprocessTargetClient, run_id: str) -> dict:
    for _ in range(80):
        health = await client.call("health", {"run_id": run_id})
        if health.get("ready"):
            return health
        await asyncio.sleep(0.25)
    raise RuntimeError(f"run did not become ready for reconnect test: {run_id}")


async def _next_log(events, *, contains: str, timeout: float) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RuntimeError(f"timed out waiting for log containing {contains!r}")
        event = await asyncio.wait_for(events.__anext__(), timeout=remaining)
        if event.get("event") != "log":
            continue
        text = str(event.get("text") or "")
        if contains in text:
            return event


async def _collect_resumed_logs(
    events,
    *,
    stop_contains: str,
    timeout: float,
) -> list[str]:
    deadline = asyncio.get_running_loop().time() + timeout
    logs: list[str] = []
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RuntimeError(f"timed out waiting for replayed log {stop_contains!r}")
        event = await asyncio.wait_for(events.__anext__(), timeout=remaining)
        if event.get("event") != "log":
            continue
        text = str(event.get("text") or "")
        logs.append(text)
        if stop_contains in text:
            return logs


async def _close_events(events) -> None:
    if events is None:
        return
    with contextlib.suppress(Exception):
        await events.aclose()


async def _main() -> None:
    port = _free_port()
    config_path.write_text(
        f"""
name: {config_name}
model: fake/model
served_model_name: fake-model
command:
  entrypoint: serve
  executable: {remote_path / "scripts" / "fake_vllm_child.py"}
server:
  host: 127.0.0.1
  port: {port}
logging:
  request_logging: false
launch:
  mode: detached
  runs_dir: {runs_dir}
  ready_timeout_seconds: 30
extra_args:
  - --sleep
  - "0.2"
""".lstrip(),
        encoding="utf-8",
    )

    run_id = f"disconnect-reconnect-{uuid.uuid4().hex}"
    first_cursor: dict[str, int] = {}
    first_text = ""
    first_seq = 0
    client = _agent_client()
    events = None
    tail_task = None
    await client.connect()
    try:
        await client.call(
            "launch",
            {
                "run_id": run_id,
                "name": config_name,
                "configs_dir": str(tmp_root),
            },
        )
        events = client.subscribe([run_id], resume_from="start")
        tail_task = asyncio.create_task(
            client.call(
                "tail_detached",
                {
                    "run_id": run_id,
                    "start_position": 0,
                    "poll_interval": 0.05,
                },
            )
        )
        first_log = await _next_log(
            events,
            contains="INFO Initializing a V1 LLM engine",
            timeout=10,
        )
        first_text = str(first_log.get("text") or "")
        first_seq = int(first_log["seq"])
        first_cursor = {
            "log_inode": first_log["log_inode"],
            "byte_offset": first_log["byte_offset"],
        }
    finally:
        await _close_events(events)
        await client.disconnect()
        if tail_task is not None:
            with contextlib.suppress(Exception):
                await tail_task

    # Let the supervised run keep writing while no controller is connected.
    await asyncio.sleep(2.5)

    client = _agent_client()
    events = None
    waited: dict = {}
    await client.connect()
    try:
        await client.call("discover_runs", {"runs_dirs": [str(runs_dir)]})
        await client.call("reattach", {"run_id": run_id})
        health = await _wait_ready(client, run_id)
        resume_request = {
            "resume_from": {
                "log_inode": first_cursor["log_inode"],
                "byte_offset": first_cursor["byte_offset"],
            },
        }
        events = client.subscribe([run_id], resume_from=resume_request["resume_from"])
        resumed_logs = await _collect_resumed_logs(
            events,
            stop_contains="INFO Uvicorn running",
            timeout=10,
        )
        expected = [
            "INFO Fetching 2 files",
            "INFO Downloading model file",
            "INFO Starting to load model",
            "INFO GPU KV cache size",
            "INFO Capturing CUDA graph shapes",
            "INFO Uvicorn running",
        ]
        missing = [
            item
            for item in expected
            if not any(item in text for text in resumed_logs)
        ]
        if missing:
            raise RuntimeError(
                f"reconnect replay missed logs {missing}; got {resumed_logs}"
            )
        if first_text in resumed_logs:
            raise RuntimeError("reconnect replay duplicated the pre-disconnect cursor")
        await _close_events(events)
        events = None
        await client.call(
            "stop",
            {
                "run_id": run_id,
                "interrupt_timeout": 2,
                "terminate_timeout": 2,
            },
        )
        waited = await client.call("wait", {"run_id": run_id})
    finally:
        await _close_events(events)
        if client.connected:
            with contextlib.suppress(Exception):
                await client.call(
                    "stop",
                    {
                        "run_id": run_id,
                        "interrupt_timeout": 1,
                        "terminate_timeout": 1,
                    },
                )
            await client.disconnect()

    shutil.rmtree(tmp_root, ignore_errors=True)
    print(
        "DISCONNECT_RECONNECT_RESUME_OK "
        f"run_id={run_id} first_seq={first_seq} "
        f"resume_inode={first_cursor['log_inode']} "
        f"resume_offset={first_cursor['byte_offset']} "
        f"url={health.get('reachable_url')} "
        f"returncode={waited.get('returncode')}"
    )


asyncio.run(_main())
PY

if [[ -n "$remote_build_spec" ]]; then
  echo "== Real build install =="
  "$venv_bin/vllm-loader" build add "${target_args[@]}" \
    --method "$remote_build_method" \
    --label "$remote_build_label" \
    --spec "$remote_build_spec"
  "$venv_bin/vllm-loader" build verify "$remote_build_label" "${target_args[@]}"
fi

if [[ -n "$remote_model_repo" ]]; then
  echo "== Real model pin =="
  if [[ -n "$remote_model_revision" ]]; then
    "$venv_bin/vllm-loader" model pin "$remote_model_id" "${target_args[@]}" \
      --repo-id "$remote_model_repo" \
      --revision "$remote_model_revision"
  else
    "$venv_bin/vllm-loader" model pin "$remote_model_id" "${target_args[@]}" \
      --repo-id "$remote_model_repo"
  fi
  remote_model_ref="$remote_model_id"
fi

if [[ -n "$remote_model_ref" ]]; then
  echo "== Real model download =="
  if [[ -n "$remote_model_revision" ]]; then
    "$venv_bin/vllm-loader" model download "$remote_model_ref" "${target_args[@]}" \
      --revision "$remote_model_revision"
  else
    "$venv_bin/vllm-loader" model download "$remote_model_ref" "${target_args[@]}"
  fi
  "$venv_bin/vllm-loader" model verify "$remote_model_ref" "${target_args[@]}"
fi

if [[ -n "$remote_real_resume_config" ]]; then
  echo "== Real model resume/daemon restart =="
  real_resume_args=("$remote_real_resume_config" "${target_args[@]}" --timeout "$remote_timeout")
  if [[ -n "$remote_build_spec" ]]; then
    real_resume_args+=(--build "$remote_build_label")
  fi
  if [[ -n "$remote_model_ref" ]]; then
    real_resume_args+=(--model-ref "$remote_model_ref")
  fi
  if [[ -n "$remote_model_revision" ]]; then
    real_resume_args+=(--revision "$remote_model_revision")
  fi
  "$venv_python" scripts/real_model_resume_check.py "${real_resume_args[@]}"
fi

if [[ -n "$real_config" ]]; then
  "$venv_bin/vllm-loader" preview "$real_config" "${target_args[@]}"
  timeout "$remote_timeout" "$venv_bin/vllm-loader" smoke-tui "$real_config" "${target_args[@]}"
fi
REMOTE
}

if [[ "$artifact_enabled" == "1" ]]; then
  mkdir -p "$artifact_dir"
  start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  date_slug="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
  host_slug="$(_validation_slug "$host")"
  config_slug=""
  if [[ -n "$real_config" ]]; then
    config_slug="-$(_validation_slug "$real_config")"
  fi
  if [[ -z "$artifact_name" ]]; then
    artifact_name="${date_slug}-${host_slug}${config_slug}-remote-validation.md"
  fi
  artifact_path="$artifact_dir/$artifact_name"
  artifact_tmp="${artifact_path}.tmp"
  local_head="$(git rev-parse --short HEAD 2>/dev/null || printf 'unknown')"
  local_head_full="$(git rev-parse HEAD 2>/dev/null || printf 'unknown')"
  printf -v ssh_preview '%q ' "${ssh_cmd[@]}"
  {
    echo "# vLLM Loader Remote Validation"
    echo
    echo "- Started: \`$start_utc\`"
    echo "- Local commit: \`$local_head\` (\`$local_head_full\`)"
    echo "- Host: \`$host\`"
    echo "- Remote path: \`$remote_path\`"
    echo "- Remote venv: \`$remote_venv\`"
    echo "- Pytest args: \`${remote_pytest_args:--q}\`"
    if [[ -n "$remote_target" ]]; then
      echo "- Remote target: \`$remote_target\`"
    else
      echo "- Remote target: _(default)_"
    fi
    echo "- Timeout: \`$remote_timeout\` seconds"
    if [[ -n "$real_config" ]]; then
      echo "- Real config: \`$real_config\`"
    else
      echo "- Real config: _(none)_"
    fi
    if [[ -n "$remote_real_resume_config" ]]; then
      echo "- Real resume validation: \`$remote_real_resume_config\`"
    else
      echo "- Real resume validation: _(not requested)_"
    fi
    if [[ -n "$remote_build_spec" ]]; then
      echo "- Build validation: \`$remote_build_method $remote_build_spec\` as \`$remote_build_label\`"
    else
      echo "- Build validation: _(not requested)_"
    fi
    if [[ -n "$remote_model_repo" ]]; then
      echo "- Model validation: pin/download \`$remote_model_repo\` as \`$remote_model_id\`"
    elif [[ -n "$remote_model_ref" ]]; then
      echo "- Model validation: download existing \`$remote_model_ref\`"
    else
      echo "- Model validation: _(not requested)_"
    fi
    echo "- SSH command: \`$ssh_preview\`"
    echo
    echo "## Output"
    echo
    echo '```text'
  } >"$artifact_tmp"
  set +e
  run_remote_validation 2>&1 | tee -a "$artifact_tmp"
  status="${PIPESTATUS[0]}"
  set -e
  completed_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  {
    echo '```'
    echo
    echo "## Result"
    echo
    echo "- Completed: \`$completed_utc\`"
    echo "- Exit status: \`$status\`"
  } >>"$artifact_tmp"
  mv "$artifact_tmp" "$artifact_path"
  echo "remote validation artifact: $artifact_path" >&2
  exit "$status"
fi

run_remote_validation
