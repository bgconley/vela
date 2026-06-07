# Vela Remote Validation

- Started: `2026-06-07T05:19:06Z`
- Local commit: `e3a5c6d` (`e3a5c6d889a02dedba38d2dc5aaf3deff89637c7`)
- Host: `bgconley@10.25.0.50`
- Remote path: `/home/bgconley/repos/lab-tui`
- Remote venv: `/tank/venvs/lab-tui`
- Pytest args: `-q`
- Remote target: `blackbird`
- Timeout: `2700` seconds
- Real config: `qwen36-27b-fp8-kvfp8-rp6000-blackbird`
- Real resume validation: _(not requested)_
- Build validation: _(not requested)_
- Model validation: _(not requested)_
- Gated model auth validation: _(not requested)_
- SSH command: `ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 -o BatchMode=yes bgconley@10.25.0.50 env VELA_REMOTE_TARGET=blackbird bash -s -- /home/bgconley/repos/lab-tui 2700 auto /tank/venvs/lab-tui qwen36-27b-fp8-kvfp8-rp6000-blackbird `

## Output

```text
== Remote git pull ==
From https://github.com/bgconley/vela
 * branch            main       -> FETCH_HEAD
   d0ca4e6..e3a5c6d  main       -> origin/main
Updating d0ca4e6..e3a5c6d
Fast-forward
 .wolf/anatomy.md                           |    50 +-
 .wolf/buglog.json                          |   551 +-
 .wolf/memory.md                            |   129 +
 .wolf/token-ledger.json                    | 23780 ++++++++++++++++++++++++++-
 docs/agent-rpc.md                          |    20 +-
 docs/configuration.md                      |    49 +
 docs/deployments.md                        |     4 +
 docs/docker-runtime.md                     |     6 +
 pyproject.toml                             |     1 +
 scripts/backend_evidence_check.py          |   314 +
 scripts/run_remote_tests.sh                |    12 +-
 src/vela/__main__.py                       |     6 +
 src/vela/agent/auth.py                     |    38 +-
 src/vela/agent/local.py                    |   141 +-
 src/vela/cli.py                            |   786 +-
 src/vela/config/targets.py                 |    18 +
 src/vela/engine/composer.py                |   143 +-
 src/vela/engine/model_registry.py          |   123 +-
 src/vela/remediation.py                    |    89 +
 src/vela/transport/client.py               |     1 +
 src/vela/transport/factory.py              |    14 +-
 src/vela/transport/ssh_bootstrap.py        |    96 +
 src/vela/transport/ssh_discovery.py        |   249 +
 src/vela/transport/ssh_setup.py            |    62 +
 src/vela/tui/app.py                        |   711 +-
 src/vela/tui/screens/flag_manager.py       |   266 +-
 src/vela/tui/screens/new_deployment.py     |   584 +-
 src/vela/tui/screens/target_edit.py        |    11 +-
 src/vela/tui/screens/target_manager.py     |    17 +
 tests/fakes/fake_docker.py                 |    41 +-
 tests/fakes/fake_ssh.py                    |   320 +
 tests/test_agent_client.py                 |   548 +-
 tests/test_cli_run.py                      |   756 +-
 tests/test_command_builder.py              |    25 +
 tests/test_deployment_composer.py          |   331 +-
 tests/test_docs.py                         |     8 +
 tests/test_fake_ssh.py                     |   202 +
 tests/test_remediation.py                  |    69 +
 tests/test_remote_workflow.py              |   212 +
 tests/test_ssh_discovery.py                |   528 +
 tests/test_targets.py                      |    11 +
 tests/test_transport_factory.py            |    39 +
 tests/test_tui_smoke.py                    |  2336 ++-
 uv.lock                                    |   781 +
 vela-docker-composer-review-findings-v6.md |   125 +
 vela-docker-composer-review-findings-v7.md |    92 +
 vela-docker-composer-review-findings-v8.md |   111 +
 vela-docker-composer-review-findings-v9.md |    82 +
 vela-session-context-2026-06-06.md         |   265 +
 vela-v1-completion-punchlist.md            |   314 +
 vllm-full-repo-review-findings-v2.md       |   101 +
 vllm-full-repo-review-findings-v3.md       |    85 +
 vllm-full-repo-review-findings-v4.md       |    64 +
 vllm-full-repo-review-findings-v5.md       |    79 +
 54 files changed, 35436 insertions(+), 360 deletions(-)
 create mode 100644 scripts/backend_evidence_check.py
 create mode 100644 src/vela/__main__.py
 create mode 100644 src/vela/remediation.py
 create mode 100644 src/vela/transport/ssh_bootstrap.py
 create mode 100644 src/vela/transport/ssh_discovery.py
 create mode 100644 src/vela/transport/ssh_setup.py
 create mode 100644 tests/fakes/fake_ssh.py
 create mode 100644 tests/test_fake_ssh.py
 create mode 100644 tests/test_remediation.py
 create mode 100644 tests/test_ssh_discovery.py
 create mode 100644 uv.lock
 create mode 100644 vela-docker-composer-review-findings-v6.md
 create mode 100644 vela-docker-composer-review-findings-v7.md
 create mode 100644 vela-docker-composer-review-findings-v8.md
 create mode 100644 vela-docker-composer-review-findings-v9.md
 create mode 100644 vela-session-context-2026-06-06.md
 create mode 100644 vela-v1-completion-punchlist.md
 create mode 100644 vllm-full-repo-review-findings-v2.md
 create mode 100644 vllm-full-repo-review-findings-v3.md
 create mode 100644 vllm-full-repo-review-findings-v4.md
 create mode 100644 vllm-full-repo-review-findings-v5.md
Obtaining file:///home/bgconley/repos/lab-tui
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
  Checking if build backend supports build_editable: started
  Checking if build backend supports build_editable: finished with status 'done'
  Getting requirements to build editable: started
  Getting requirements to build editable: finished with status 'done'
  Installing backend dependencies: started
  Installing backend dependencies: finished with status 'done'
  Preparing editable metadata (pyproject.toml): started
  Preparing editable metadata (pyproject.toml): finished with status 'done'
Requirement already satisfied: httpx>=0.27 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (0.28.1)
Collecting huggingface-hub>=0.27 (from vela==0.1.0)
  Downloading huggingface_hub-1.18.0-py3-none-any.whl.metadata (14 kB)
Requirement already satisfied: psutil>=5.9 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (7.2.2)
Requirement already satisfied: pydantic>=2.8 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (2.13.4)
Requirement already satisfied: pyyaml>=6.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (6.0.3)
Requirement already satisfied: rich>=13.7 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (15.0.0)
Requirement already satisfied: textual>=0.86 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (8.2.7)
Collecting tqdm>=4.66 (from vela==0.1.0)
  Downloading tqdm-4.68.1-py3-none-any.whl.metadata (57 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 57.8/57.8 kB 1.7 MB/s eta 0:00:00
Requirement already satisfied: typer>=0.12 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (0.26.6)
Requirement already satisfied: pytest-asyncio>=0.24 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (1.4.0)
Requirement already satisfied: pytest>=8.2 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (9.0.3)
Requirement already satisfied: ruff>=0.6 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (0.15.15)
Requirement already satisfied: anyio in /tank/venvs/lab-tui/lib/python3.12/site-packages (from httpx>=0.27->vela==0.1.0) (4.13.0)
Requirement already satisfied: certifi in /tank/venvs/lab-tui/lib/python3.12/site-packages (from httpx>=0.27->vela==0.1.0) (2026.5.20)
Requirement already satisfied: httpcore==1.* in /tank/venvs/lab-tui/lib/python3.12/site-packages (from httpx>=0.27->vela==0.1.0) (1.0.9)
Requirement already satisfied: idna in /tank/venvs/lab-tui/lib/python3.12/site-packages (from httpx>=0.27->vela==0.1.0) (3.18)
Requirement already satisfied: h11>=0.16 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from httpcore==1.*->httpx>=0.27->vela==0.1.0) (0.16.0)
Collecting click>=8.4.0 (from huggingface-hub>=0.27->vela==0.1.0)
  Using cached click-8.4.1-py3-none-any.whl.metadata (2.6 kB)
Collecting filelock>=3.10.0 (from huggingface-hub>=0.27->vela==0.1.0)
  Using cached filelock-3.29.1-py3-none-any.whl.metadata (2.0 kB)
Collecting fsspec>=2023.5.0 (from huggingface-hub>=0.27->vela==0.1.0)
  Using cached fsspec-2026.4.0-py3-none-any.whl.metadata (10 kB)
Collecting hf-xet<2.0.0,>=1.4.3 (from huggingface-hub>=0.27->vela==0.1.0)
  Using cached hf_xet-1.5.0-cp37-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl.metadata (4.9 kB)
Requirement already satisfied: packaging>=20.9 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (26.2)
Collecting typer>=0.12 (from vela==0.1.0)
  Using cached typer-0.25.1-py3-none-any.whl.metadata (15 kB)
Requirement already satisfied: typing-extensions>=4.1.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (4.15.0)
Requirement already satisfied: annotated-types>=0.6.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from pydantic>=2.8->vela==0.1.0) (0.7.0)
Requirement already satisfied: pydantic-core==2.46.4 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from pydantic>=2.8->vela==0.1.0) (2.46.4)
Requirement already satisfied: typing-inspection>=0.4.2 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from pydantic>=2.8->vela==0.1.0) (0.4.2)
Requirement already satisfied: iniconfig>=1.0.1 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from pytest>=8.2->vela==0.1.0) (2.3.0)
Requirement already satisfied: pluggy<2,>=1.5 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from pytest>=8.2->vela==0.1.0) (1.6.0)
Requirement already satisfied: pygments>=2.7.2 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from pytest>=8.2->vela==0.1.0) (2.20.0)
Requirement already satisfied: markdown-it-py>=2.2.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from rich>=13.7->vela==0.1.0) (4.2.0)
Requirement already satisfied: mdit-py-plugins in /tank/venvs/lab-tui/lib/python3.12/site-packages (from textual>=0.86->vela==0.1.0) (0.6.1)
Requirement already satisfied: platformdirs<5,>=3.6.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from textual>=0.86->vela==0.1.0) (4.10.0)
Requirement already satisfied: shellingham>=1.3.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from typer>=0.12->vela==0.1.0) (1.5.4)
Requirement already satisfied: annotated-doc>=0.0.2 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from typer>=0.12->vela==0.1.0) (0.0.4)
Requirement already satisfied: mdurl~=0.1 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from markdown-it-py>=2.2.0->rich>=13.7->vela==0.1.0) (0.1.2)
Requirement already satisfied: linkify-it-py<3,>=1 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from markdown-it-py[linkify]>=2.1.0->textual>=0.86->vela==0.1.0) (2.1.0)
Requirement already satisfied: uc-micro-py in /tank/venvs/lab-tui/lib/python3.12/site-packages (from linkify-it-py<3,>=1->markdown-it-py[linkify]>=2.1.0->textual>=0.86->vela==0.1.0) (2.0.0)
Downloading huggingface_hub-1.18.0-py3-none-any.whl (684 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 684.4/684.4 kB 10.5 MB/s eta 0:00:00
Downloading tqdm-4.68.1-py3-none-any.whl (78 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 78.4/78.4 kB 4.9 MB/s eta 0:00:00
Using cached typer-0.25.1-py3-none-any.whl (58 kB)
Using cached click-8.4.1-py3-none-any.whl (116 kB)
Using cached filelock-3.29.1-py3-none-any.whl (40 kB)
Using cached fsspec-2026.4.0-py3-none-any.whl (203 kB)
Using cached hf_xet-1.5.0-cp37-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl (4.5 MB)
Checking if build backend supports build_editable: started
Checking if build backend supports build_editable: finished with status 'done'
Building wheels for collected packages: vela
  Building editable for vela (pyproject.toml): started
  Building editable for vela (pyproject.toml): finished with status 'done'
  Created wheel for vela: filename=vela-0.1.0-py3-none-any.whl size=4899 sha256=19fc5b934c131abb7b8712eb59e636c671644e50e228d047e87d7744898dee31
  Stored in directory: /tmp/pip-ephem-wheel-cache-q58raqh7/wheels/3e/99/96/55613bfa0cc7c533e6eb1028217acc00841afc206a5665afde
Successfully built vela
Installing collected packages: tqdm, hf-xet, fsspec, filelock, click, typer, huggingface-hub, vela
  Attempting uninstall: typer
    Found existing installation: typer 0.26.6
    Uninstalling typer-0.26.6:
      Successfully uninstalled typer-0.26.6
Successfully installed click-8.4.1 filelock-3.29.1 fsspec-2026.4.0 hf-xet-1.5.0 huggingface-hub-1.18.0 tqdm-4.68.1 typer-0.25.1 vela-0.1.0
== Remote agent restart ==
running pid=2986238 socket=/run/user/1000/vela/agent.sock
== Remote host ==
620-01
GPU unavailable=False note=
GPU 0 NVIDIA RTX PRO 4000 Blackwell GPU-103aae53-b1be-c275-656e-82515763d051 mem=22003/24467MiB util=0%
GPU 1 NVIDIA RTX PRO 4000 Blackwell GPU-6ec4ee66-142e-34ad-e17d-a131d7153b51 mem=23559/24467MiB util=0%
vllm not found on PATH; no-GPU package checks will still run
All checks passed!
...................................................F.......F....F....... [  7%]
..F.FF........................................F..F...........F.......... [ 15%]
........................................................................ [ 23%]
...........FF.FFF....................................................... [ 30%]
........................................F............................... [ 38%]
........................................................................ [ 46%]
........................................................................ [ 53%]
........................................................................ [ 61%]
........................................................................ [ 69%]
........................................................................ [ 77%]
........................................................................ [ 84%]
........................................................................ [ 92%]
............F....F....................................................   [100%]
=================================== FAILURES ===================================
_____ test_local_agent_prepare_launch_uses_command_cwd_for_relative_model ______

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_local_agent_prepare_launc0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_local_agent_prepare_launc0')

    @pytest.mark.asyncio
    async def test_local_agent_prepare_launch_uses_command_cwd_for_relative_model(
        config_dir: Path, tmp_path: Path
    ) -> None:
        work_dir = tmp_path / "serve-root"
        model_dir = work_dir / "relative-model"
        model_dir.mkdir(parents=True)
        write_yaml(
            config_dir / "cwd-local.yaml",
            f"""
            name: cwd-local
            model: relative-model
            command:
              cwd: {work_dir}
            """,
        )
        client = InProcessTargetClient(LocalAgent())
    
        await client.connect()
>       result = await client.call(
            "prepare_launch",
            {"name": "cwd-local", "configs_dir": str(config_dir)},
        )

tests/test_agent_client.py:1297: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:440: in handle
    return self._prepare_launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccacc0ef0>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_local_agent_prepare_launc0/configs', 'name': 'cwd-local'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
___________ test_local_agent_starts_and_stops_attached_run_by_run_id ___________

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_local_agent_starts_and_st0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_local_agent_starts_and_st0')

    @pytest.mark.asyncio
    async def test_local_agent_starts_and_stops_attached_run_by_run_id(
        config_dir: Path, tmp_path: Path
    ) -> None:
        marker = tmp_path / "marker.txt"
        child = tmp_path / "child.py"
        child.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import signal",
                    "import time",
                    "from pathlib import Path",
                    f"marker = Path({str(marker)!r})",
                    "marker.write_text('started', encoding='utf-8')",
                    "def stop(signum, frame):",
                    "    marker.write_text('stopped', encoding='utf-8')",
                    "    raise SystemExit(0)",
                    "signal.signal(signal.SIGINT, stop)",
                    "while True:",
                    "    time.sleep(0.05)",
                ]
            ),
            encoding="utf-8",
        )
        child.chmod(0o755)
        write_yaml(
            config_dir / "attached.yaml",
            f"""
            name: attached
            model: fake/model
            command:
              entrypoint: serve
              executable: {child}
            launch:
              runs_dir: {tmp_path / "runs"}
            """,
        )
        agent = LocalAgent()
        client = InProcessTargetClient(agent)
        await client.connect()
        run_id: str | None = None
        try:
>           launch = await client.call(
                "launch", {"name": "attached", "configs_dir": str(config_dir)}
            )

tests/test_agent_client.py:1563: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:442: in handle
    return self._launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:1357: in _launch
    prepared = self._prepare_launch(params)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccab240e0>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_local_agent_starts_and_st0/configs', 'name': 'attached'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
_____________ test_local_agent_emits_attached_log_and_phase_events _____________

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_local_agent_emits_attache0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_local_agent_emits_attache0')

    @pytest.mark.asyncio
    async def test_local_agent_emits_attached_log_and_phase_events(
        config_dir: Path, tmp_path: Path
    ) -> None:
        child = tmp_path / "child.py"
        child.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "print('INFO Starting to load model', flush=True)",
                ]
            ),
            encoding="utf-8",
        )
        child.chmod(0o755)
        write_yaml(
            config_dir / "events.yaml",
            f"""
            name: events
            model: fake/model
            command:
              entrypoint: serve
              executable: {child}
            launch:
              runs_dir: {tmp_path / "runs"}
            """,
        )
        assert not hasattr(local_agent_module, "start_attached")
        client = InProcessTargetClient(LocalAgent())
        await client.connect()
        try:
>           launch = await client.call(
                "launch", {"name": "events", "configs_dir": str(config_dir)}
            )

tests/test_agent_client.py:1871: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:442: in handle
    return self._launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:1357: in _launch
    prepared = self._prepare_launch(params)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccabd6d80>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_local_agent_emits_attache0/configs', 'name': 'events'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
____________ test_local_agent_records_build_ref_for_detached_launch ____________

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_local_agent_records_build0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_local_agent_records_build0')
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x713ccabc89e0>

    @pytest.mark.asyncio
    async def test_local_agent_records_build_ref_for_detached_launch(
        config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        builds_root = tmp_path / "data" / "vela" / "builds"
        build_dir = builds_root / "01REFBUILD"
        bin_dir = build_dir / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
        (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        (build_dir / "build.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "build_id": "01REFBUILD",
                    "label": "ref-build",
                    "status": "ready",
                    "paths": {
                        "root": str(build_dir),
                        "venv": "venv",
                        "executable": "bin/vllm",
                        "python": "bin/python",
                    },
                }
            ),
            encoding="utf-8",
        )
        write_yaml(
            config_dir / "build-ref.yaml",
            f"""
            name: build-ref
            model: fake/model
            command:
              build: ref-build
            launch:
              mode: detached
              runs_dir: {tmp_path / "runs"}
            """,
        )
        sidecar_path = tmp_path / "runs" / "run-1.json"
        manifest_path = tmp_path / "runs" / "run-1.manifest.json"
        log_path = tmp_path / "runs" / "run-1.run.log"
    
        def fake_start_detached(cfg, build, **_kwargs):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")
            Manifest.from_active_log(log_path).write_atomic(manifest_path)
            Sidecar(
                run_id="run-1",
                config_name=cfg.name,
                command_argv=list(build.argv),
                command_hash="sha256:abc",
                pid=123,
                pgid=123,
                process_create_time=1.0,
                executable=str(build.argv[0]),
                cwd=str(build.cwd),
                launch_mode=cfg.launch.mode.value,
                host=cfg.server.host,
                port=cfg.server.port,
                served_model_names=[cfg.served_model_name]
                if cfg.served_model_name
                else [],
                exposure=cfg.server.exposure.value,
                manifest_path=str(manifest_path),
                config_snapshot=cfg.model_dump(mode="json"),
            ).write_atomic(sidecar_path)
            return DetachedLaunch(
                run_id="run-1",
                supervisor_pid=123,
                sidecar_path=sidecar_path,
                manifest_path=manifest_path,
                log_path=log_path,
            )
    
        monkeypatch.setattr(local_agent_module, "start_detached", fake_start_detached)
        client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
        await client.connect()
    
        try:
>           await client.call("launch", {"name": "build-ref", "configs_dir": str(config_dir)})

tests/test_agent_client.py:2522: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:442: in handle
    return self._launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:1357: in _launch
    prepared = self._prepare_launch(params)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccabd9a30>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_local_agent_records_build0/configs', 'name': 'build-ref'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
__________ test_target_client_detached_launch_can_reattach_by_run_id ___________

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_detached_la0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_detached_la0')
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x713ccabb35f0>

    @pytest.mark.asyncio
    async def test_target_client_detached_launch_can_reattach_by_run_id(
        config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executable = tmp_path / "child.py"
        executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        executable.chmod(0o755)
        write_yaml(
            config_dir / "detached-wire.yaml",
            f"""
            name: detached-wire
            model: fake/model
            command:
              entrypoint: serve
              executable: {executable}
            launch:
              mode: detached
              runs_dir: {tmp_path / "runs"}
            """,
        )
        sidecar_path = tmp_path / "runs" / "run-1.json"
        log_path = tmp_path / "runs" / "run-1.run.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        manifest = Manifest.from_active_log(log_path)
        sidecar = Sidecar(
            run_id="run-1",
            config_name="detached-wire",
            command_argv=["vllm", "serve", "fake/model"],
            command_hash="sha256:abc",
            pid=123,
            pgid=123,
            process_create_time=1.0,
            executable="/bin/vllm",
            cwd=str(tmp_path),
            launch_mode="detached",
            host="127.0.0.1",
            port=8000,
            served_model_names=["served"],
            exposure="local",
            manifest_path=str(tmp_path / "runs" / "run-1.manifest.json"),
            config_snapshot={"name": "detached-wire", "model": "fake/model"},
        )
    
        monkeypatch.setattr(
            local_agent_module,
            "start_detached",
            lambda *_args, **_kwargs: DetachedLaunch(
                run_id="run-1",
                supervisor_pid=123,
                sidecar_path=sidecar_path,
                manifest_path=tmp_path / "runs" / "run-1.manifest.json",
                log_path=log_path,
            ),
        )
        monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", lambda path: True)
        monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
        monkeypatch.setattr(local_agent_module, "load_manifest", lambda path: manifest)
        client = InProcessTargetClient(LocalAgent())
        await client.connect()
    
>       launch = await client.call(
            "launch", {"name": "detached-wire", "configs_dir": str(config_dir)}
        )

tests/test_agent_client.py:2663: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:442: in handle
    return self._launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:1357: in _launch
    prepared = self._prepare_launch(params)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccabb01d0>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_target_client_detached_la0/configs', 'name': 'detached-wire'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
_____ test_target_client_detached_launch_is_idempotent_by_requested_run_id _____

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_detached_la1/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_detached_la1')
monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x713ccab8ade0>

    @pytest.mark.asyncio
    async def test_target_client_detached_launch_is_idempotent_by_requested_run_id(
        config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executable = tmp_path / "child.py"
        executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        executable.chmod(0o755)
        write_yaml(
            config_dir / "detached-idempotent.yaml",
            f"""
            name: detached-idempotent
            model: fake/model
            command:
              entrypoint: serve
              executable: {executable}
            launch:
              mode: detached
              runs_dir: {tmp_path / "runs"}
            """,
        )
        starts: list[str | None] = []
    
        def fake_start_detached(cfg, build, *_, run_id=None, **_kwargs) -> DetachedLaunch:
            starts.append(run_id)
            actual_run_id = str(run_id or "generated")
            log_path = tmp_path / "runs" / f"{actual_run_id}.run.log"
            manifest_path = tmp_path / "runs" / f"{actual_run_id}.manifest.json"
            sidecar_path = tmp_path / "runs" / f"{actual_run_id}.json"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")
            Manifest.from_active_log(log_path).write_atomic(manifest_path)
            Sidecar(
                run_id=actual_run_id,
                config_name=cfg.name,
                command_argv=list(build.argv),
                command_hash="sha256:abc",
                pid=123,
                pgid=123,
                process_create_time=1.0,
                executable=str(build.argv[0]),
                cwd=str(build.cwd),
                launch_mode=cfg.launch.mode.value,
                host=cfg.server.host,
                port=cfg.server.port,
                served_model_names=[cfg.served_model_name]
                if cfg.served_model_name
                else [],
                exposure=cfg.server.exposure.value,
                manifest_path=str(manifest_path),
                config_snapshot=cfg.model_dump(mode="json"),
            ).write_atomic(sidecar_path)
            return DetachedLaunch(
                run_id=actual_run_id,
                supervisor_pid=123,
                sidecar_path=sidecar_path,
                manifest_path=manifest_path,
                log_path=log_path,
            )
    
        monkeypatch.setattr(local_agent_module, "start_detached", fake_start_detached)
        client = InProcessTargetClient(LocalAgent())
        await client.connect()
    
>       first = await client.call(
            "launch",
            {
                "name": "detached-idempotent",
                "configs_dir": str(config_dir),
                "run_id": "detached-idem-1",
            },
        )

tests/test_agent_client.py:2742: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:442: in handle
    return self._launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:1357: in _launch
    prepared = self._prepare_launch(params)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccab8a7e0>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_target_client_detached_la1/configs', 'name': 'detached-idempotent', 'run_id': 'detached-idem-1'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
___________ test_agent_prepare_launch_resolves_pinned_build_handoff ____________

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_agent_prepare_launch_reso0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_agent_prepare_launch_reso0')

    @pytest.mark.asyncio
    async def test_agent_prepare_launch_resolves_pinned_build_handoff(
        config_dir: Path, tmp_path: Path
    ) -> None:
        builds_root = tmp_path / "data" / "vela" / "builds"
        build_dir = builds_root / "01BUILDREADY"
        bin_dir = build_dir / "bin"
        bin_dir.mkdir(parents=True)
        vllm_bin = bin_dir / "vllm"
        python_bin = bin_dir / "python"
        vllm_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        python_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        vllm_bin.chmod(0o755)
        python_bin.chmod(0o755)
        (build_dir / "build.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "build_id": "01BUILDREADY",
                    "label": "nightly-cu130",
                    "status": "ready",
                    "resolved": {
                        "vllm": "0.17.0.dev",
                        "vllm_version_profile": "current",
                    },
                    "paths": {
                        "root": str(build_dir),
                        "venv": "venv",
                        "executable": "bin/vllm",
                        "python": "bin/python",
                        "activate": "activate",
                        "run_script": "run.sh",
                    },
                }
            ),
            encoding="utf-8",
        )
        write_yaml(
            config_dir / "built.yaml",
            """
            name: built
            model: org/model
            command:
              build: nightly-cu130
            """,
        )
    
        client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
        await client.connect()
        try:
>           prepared = await client.call(
                "prepare_launch", {"name": "built", "configs_dir": str(config_dir)}
            )

tests/test_agent_client.py:5030: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:440: in handle
    return self._prepare_launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccacc0d10>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_agent_prepare_launch_reso0/configs', 'name': 'built'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
____ test_agent_prepare_launch_resolves_build_python_for_module_entrypoint _____

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_agent_prepare_launch_reso1/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_agent_prepare_launch_reso1')

    @pytest.mark.asyncio
    async def test_agent_prepare_launch_resolves_build_python_for_module_entrypoint(
        config_dir: Path, tmp_path: Path
    ) -> None:
        builds_root = tmp_path / "data" / "vela" / "builds"
        build_dir = builds_root / "01MODULEBUILD"
        bin_dir = build_dir / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
        python_bin = bin_dir / "python"
        python_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        (bin_dir / "vllm").chmod(0o755)
        python_bin.chmod(0o755)
        (build_dir / "build.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "build_id": "01MODULEBUILD",
                    "label": "module-build",
                    "status": "adopted",
                    "resolved": {"vllm_version_profile": "current"},
                    "paths": {
                        "root": str(build_dir),
                        "venv": "venv",
                        "executable": "bin/vllm",
                        "python": "bin/python",
                    },
                }
            ),
            encoding="utf-8",
        )
        write_yaml(
            config_dir / "module.yaml",
            """
            name: module
            model: org/model
            command:
              entrypoint: module
              build: 01MODULEBUILD
            """,
        )
    
        client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
        await client.connect()
        try:
>           prepared = await client.call(
                "prepare_launch", {"name": "module", "configs_dir": str(config_dir)}
            )

tests/test_agent_client.py:5231: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:440: in handle
    return self._prepare_launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccabda5d0>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_agent_prepare_launch_reso1/configs', 'name': 'module'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
____________ test_agent_adopts_local_model_path_for_launch_handoff _____________

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_agent_adopts_local_model_0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_agent_adopts_local_model_0')

    @pytest.mark.asyncio
    async def test_agent_adopts_local_model_path_for_launch_handoff(
        config_dir: Path, tmp_path: Path
    ) -> None:
        registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
        model_dir = tmp_path / "models" / "local-llama"
        model_dir.mkdir(parents=True)
        (model_dir / "config.json").write_text("{}", encoding="utf-8")
        (model_dir / "model.safetensors").write_text("weights", encoding="utf-8")
        (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
        write_yaml(
            config_dir / "local-model.yaml",
            """
            name: local-model
            model: local-llama
            model_ref: 01LOCAL
            """,
        )
    
        client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
        await client.connect()
        try:
            adopted = await client.call(
                "pin_model",
                {
                    "entry_id": "01LOCAL",
                    "display_name": "local-llama",
                    "source": "local_path",
                    "local_path": str(model_dir),
                },
            )
>           prepared = await client.call(
                "prepare_launch",
                {"name": "local-model", "configs_dir": str(config_dir)},
            )

tests/test_agent_client.py:5921: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:440: in handle
    return self._prepare_launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccabc4500>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_agent_adopts_local_model_0/configs', 'name': 'local-model'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
_______ test_target_client_launches_attached_run_with_serialized_events ________

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_launches_at0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_launches_at0')

    @pytest.mark.asyncio
    async def test_target_client_launches_attached_run_with_serialized_events(
        config_dir: Path, tmp_path: Path
    ) -> None:
        child = tmp_path / "child.py"
        child.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "print('INFO Starting to load model', flush=True)",
                ]
            ),
            encoding="utf-8",
        )
        child.chmod(0o755)
        write_yaml(
            config_dir / "wire.yaml",
            f"""
            name: wire
            model: fake/model
            command:
              entrypoint: serve
              executable: {child}
            launch:
              runs_dir: {tmp_path / "runs"}
            """,
        )
        client = InProcessTargetClient(LocalAgent())
        await client.connect()
    
>       launch = await client.call(
            "launch",
            {"name": "wire", "configs_dir": str(config_dir), "run_id": "run-wire-1"},
        )

tests/test_agent_client.py:10725: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:442: in handle
    return self._launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:1357: in _launch
    prepared = self._prepare_launch(params)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccc5b9b20>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_target_client_launches_at0/configs', 'name': 'wire', 'run_id': 'run-wire-1'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
_________ test_target_client_replays_buffered_run_events_from_sequence _________

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_replays_buf0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_replays_buf0')

    @pytest.mark.asyncio
    async def test_target_client_replays_buffered_run_events_from_sequence(
        config_dir: Path, tmp_path: Path
    ) -> None:
        child = tmp_path / "child.py"
        child.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "print('INFO Starting to load model', flush=True)",
                    "print('INFO Uvicorn running on http://127.0.0.1:8000', flush=True)",
                ]
            ),
            encoding="utf-8",
        )
        child.chmod(0o755)
        write_yaml(
            config_dir / "replay.yaml",
            f"""
            name: replay
            model: fake/model
            command:
              entrypoint: serve
              executable: {child}
            launch:
              runs_dir: {tmp_path / "runs"}
            """,
        )
        client = InProcessTargetClient(LocalAgent())
        await client.connect()
    
>       await client.call(
            "launch",
            {"name": "replay", "configs_dir": str(config_dir), "run_id": "run-replay-1"},
        )

tests/test_agent_client.py:10795: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:442: in handle
    return self._launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:1357: in _launch
    prepared = self._prepare_launch(params)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccac05370>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_target_client_replays_buf0/configs', 'name': 'replay', 'run_id': 'run-replay-1'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
__________ test_target_client_replays_durable_log_events_from_offset ___________

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_replays_dur0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_replays_dur0')

    @pytest.mark.asyncio
    async def test_target_client_replays_durable_log_events_from_offset(
        config_dir: Path, tmp_path: Path
    ) -> None:
        child = tmp_path / "child.py"
        child.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "print('INFO first line', flush=True)",
                    "print('INFO second line', flush=True)",
                ]
            ),
            encoding="utf-8",
        )
        child.chmod(0o755)
        write_yaml(
            config_dir / "offset-replay.yaml",
            f"""
            name: offset-replay
            model: fake/model
            command:
              entrypoint: serve
              executable: {child}
            launch:
              runs_dir: {tmp_path / "runs"}
            """,
        )
        client = InProcessTargetClient(LocalAgent())
        await client.connect()
    
>       await client.call(
            "launch",
            {
                "name": "offset-replay",
                "configs_dir": str(config_dir),
                "run_id": "run-offset-replay-1",
            },
        )

tests/test_agent_client.py:10864: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:442: in handle
    return self._launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:1357: in _launch
    prepared = self._prepare_launch(params)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccc5bb9b0>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_target_client_replays_dur0/configs', 'name': 'offset-replay', 'run_id': 'run-offset-replay-1'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
________ test_target_client_replays_from_new_active_log_after_rotation _________

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_replays_fro0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_replays_fro0')

    @pytest.mark.asyncio
    async def test_target_client_replays_from_new_active_log_after_rotation(
        config_dir: Path, tmp_path: Path
    ) -> None:
        child = tmp_path / "child.py"
        child.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "print('INFO old active line', flush=True)",
                ]
            ),
            encoding="utf-8",
        )
        child.chmod(0o755)
        write_yaml(
            config_dir / "rotation-replay.yaml",
            f"""
            name: rotation-replay
            model: fake/model
            command:
              entrypoint: serve
              executable: {child}
            launch:
              runs_dir: {tmp_path / "runs"}
            """,
        )
        agent = LocalAgent()
        client = InProcessTargetClient(agent)
        await client.connect()
    
>       await client.call(
            "launch",
            {
                "name": "rotation-replay",
                "configs_dir": str(config_dir),
                "run_id": "run-rotation-replay-1",
            },
        )

tests/test_agent_client.py:10924: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:442: in handle
    return self._launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:1357: in _launch
    prepared = self._prepare_launch(params)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccc5d8ef0>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_target_client_replays_fro0/configs', 'name': 'rotation-replay', 'run_id': 'run-rotation-replay-1'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
_______________ test_target_client_kills_attached_run_by_run_id ________________

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_kills_attac0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_target_client_kills_attac0')

    @pytest.mark.asyncio
    async def test_target_client_kills_attached_run_by_run_id(
        config_dir: Path, tmp_path: Path
    ) -> None:
        child = tmp_path / "child.py"
        child.write_text(
            "#!/usr/bin/env python3\nimport time\nwhile True:\n    time.sleep(0.05)\n",
            encoding="utf-8",
        )
        child.chmod(0o755)
        write_yaml(
            config_dir / "kill-wire.yaml",
            f"""
            name: kill-wire
            model: fake/model
            command:
              entrypoint: serve
              executable: {child}
            launch:
              runs_dir: {tmp_path / "runs"}
            """,
        )
        client = InProcessTargetClient(LocalAgent())
        await client.connect()
    
>       await client.call(
            "launch",
            {
                "name": "kill-wire",
                "configs_dir": str(config_dir),
                "run_id": "run-kill-1",
            },
        )

tests/test_agent_client.py:10984: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 
src/vela/transport/inprocess.py:46: in call
    result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:442: in handle
    return self._launch(payload)
           ^^^^^^^^^^^^^^^^^^^^^
src/vela/agent/local.py:1357: in _launch
    prepared = self._prepare_launch(params)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

self = <vela.agent.local.LocalAgent object at 0x713ccab66420>
params = {'configs_dir': '/tmp/pytest-of-bgconley/pytest-153/test_target_client_kills_attac0/configs', 'name': 'kill-wire', 'run_id': 'run-kill-1'}

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(_config_by_name(registry, name), params)
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
        if failure is not None:
>           raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
E           vela.agent.local.TargetCallError: Port 8000 is already in use on 127.0.0.1.

src/vela/agent/local.py:1149: TargetCallError
__________ test_cli_run_reports_missing_executable_without_traceback ___________

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_cli_run_reports_missing_e0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_cli_run_reports_missing_e0')

    def test_cli_run_reports_missing_executable_without_traceback(
        config_dir: Path, tmp_path: Path
    ) -> None:
        missing_executable = tmp_path / "missing-vllm"
        write_yaml(
            config_dir / "missing-bin.yaml",
            f"""
            name: missing-bin
            model: fake/model
            command:
              entrypoint: serve
              executable: {missing_executable}
            """,
        )
    
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "vela.cli",
                "run",
                "missing-bin",
                "--configs-dir",
                str(config_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    
        assert proc.returncode != 0
>       assert "Command not found" in proc.stderr
E       AssertionError: assert 'Command not found' in 'ERROR PORT_IN_USE: Port 8000 is already in use on 127.0.0.1.\n'
E        +  where 'ERROR PORT_IN_USE: Port 8000 is already in use on 127.0.0.1.\n' = CompletedProcess(args=['/tank/venvs/lab-tui/bin/python', '-m', 'vela.cli', 'run', 'missing-bin', '--configs-dir', '/tm...missing_e0/configs'], returncode=2, stdout='', stderr='ERROR PORT_IN_USE: Port 8000 is already in use on 127.0.0.1.\n').stderr

tests/test_cli_run.py:3958: AssertionError
______ test_missing_executable_shows_launch_guidance_instead_of_crashing _______

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_missing_executable_shows_0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_missing_executable_shows_0')

    @pytest.mark.asyncio
    async def test_missing_executable_shows_launch_guidance_instead_of_crashing(
        config_dir: Path, tmp_path: Path
    ) -> None:
        missing_executable = tmp_path / "does-not-exist"
        write_yaml(
            config_dir / "missing-bin.yaml",
            f"""
            name: missing-bin
            model: fake/model
            command:
              entrypoint: serve
              executable: {missing_executable}
            """,
        )
        app = VelaApp(configs_dir=config_dir)
    
        async with app.run_test() as pilot:
            await pilot.pause()
            app.current_config = app.registry.by_name("missing-bin")
            await app._run_selected_config()
    
            assert app.phase is Phase.ERROR
>           assert app.fsm.error_kind is ErrorKind.COMMAND_NOT_FOUND
E           AssertionError: assert <ErrorKind.PORT_IN_USE: 'PORT_IN_USE'> is <ErrorKind.COMMAND_NOT_FOUND: 'COMMAND_NOT_FOUND'>
E            +  where <ErrorKind.PORT_IN_USE: 'PORT_IN_USE'> = <vela.engine.phases.PhaseFSM object at 0x713ccac01d30>.error_kind
E            +    where <vela.engine.phases.PhaseFSM object at 0x713ccac01d30> = VelaApp(title='VelaApp', classes={'-dark-mode'}, pseudo_classes={'focus', 'dark'}).fsm
E            +  and   <ErrorKind.COMMAND_NOT_FOUND: 'COMMAND_NOT_FOUND'> = ErrorKind.COMMAND_NOT_FOUND

tests/test_tui_smoke.py:10539: AssertionError
____________ test_detached_missing_executable_shows_launch_guidance ____________

config_dir = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_detached_missing_executab0/configs')
tmp_path = PosixPath('/tmp/pytest-of-bgconley/pytest-153/test_detached_missing_executab0')

    @pytest.mark.asyncio
    async def test_detached_missing_executable_shows_launch_guidance(
        config_dir: Path, tmp_path: Path
    ) -> None:
        missing_executable = tmp_path / "detached-missing"
        runs_dir = tmp_path / "runs"
        write_yaml(
            config_dir / "detached-missing-bin.yaml",
            f"""
            name: detached-missing-bin
            model: fake/model
            command:
              entrypoint: serve
              executable: {missing_executable}
            launch:
              mode: detached
              runs_dir: {runs_dir}
            """,
        )
        app = VelaApp(configs_dir=config_dir)
    
        async with app.run_test() as pilot:
            await pilot.pause()
            app.current_config = app.registry.by_name("detached-missing-bin")
            await app._run_selected_config()
    
            assert app.phase is Phase.ERROR
>           assert app.fsm.error_kind is ErrorKind.COMMAND_NOT_FOUND
E           AssertionError: assert <ErrorKind.PORT_IN_USE: 'PORT_IN_USE'> is <ErrorKind.COMMAND_NOT_FOUND: 'COMMAND_NOT_FOUND'>
E            +  where <ErrorKind.PORT_IN_USE: 'PORT_IN_USE'> = <vela.engine.phases.PhaseFSM object at 0x713cae8357c0>.error_kind
E            +    where <vela.engine.phases.PhaseFSM object at 0x713cae8357c0> = VelaApp(title='VelaApp', classes={'-dark-mode'}, pseudo_classes={'focus', 'dark'}).fsm
E            +  and   <ErrorKind.COMMAND_NOT_FOUND: 'COMMAND_NOT_FOUND'> = ErrorKind.COMMAND_NOT_FOUND

tests/test_tui_smoke.py:10737: AssertionError
=========================== short test summary info ============================
FAILED tests/test_agent_client.py::test_local_agent_prepare_launch_uses_command_cwd_for_relative_model
FAILED tests/test_agent_client.py::test_local_agent_starts_and_stops_attached_run_by_run_id
FAILED tests/test_agent_client.py::test_local_agent_emits_attached_log_and_phase_events
FAILED tests/test_agent_client.py::test_local_agent_records_build_ref_for_detached_launch
FAILED tests/test_agent_client.py::test_target_client_detached_launch_can_reattach_by_run_id
FAILED tests/test_agent_client.py::test_target_client_detached_launch_is_idempotent_by_requested_run_id
FAILED tests/test_agent_client.py::test_agent_prepare_launch_resolves_pinned_build_handoff
FAILED tests/test_agent_client.py::test_agent_prepare_launch_resolves_build_python_for_module_entrypoint
FAILED tests/test_agent_client.py::test_agent_adopts_local_model_path_for_launch_handoff
FAILED tests/test_agent_client.py::test_target_client_launches_attached_run_with_serialized_events
FAILED tests/test_agent_client.py::test_target_client_replays_buffered_run_events_from_sequence
FAILED tests/test_agent_client.py::test_target_client_replays_durable_log_events_from_offset
FAILED tests/test_agent_client.py::test_target_client_replays_from_new_active_log_after_rotation
FAILED tests/test_agent_client.py::test_target_client_kills_attached_run_by_run_id
FAILED tests/test_cli_run.py::test_cli_run_reports_missing_executable_without_traceback
FAILED tests/test_tui_smoke.py::test_missing_executable_shows_launch_guidance_instead_of_crashing
FAILED tests/test_tui_smoke.py::test_detached_missing_executable_shows_launch_guidance
17 failed, 917 passed in 136.50s (0:02:16)
```

## Result

- Completed: `2026-06-07T05:21:31Z`
- Exit status: `1`
