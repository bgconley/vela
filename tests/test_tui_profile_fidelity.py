from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from conftest import write_yaml
from textual.app import App

from vela.agent import local as local_agent_module
from vela.agent.local import LocalAgent
from vela.config.loader import load_registry
from vela.config.schema import ModelConfig
from vela.transport.inprocess import InProcessTargetClient
from vela.tui.app import VelaApp
from vela.tui.screens.config_picker import ConfigPickerScreen
from vela.tui.screens.model_manager import _FOOTER_HINTS, _model_selection_payload


class _Host(App):
    pass


class _LaunchClient:
    def __init__(self) -> None:
        self.connected = False
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.launch_count = 0

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, dict(params)))
        if method == "list_configs":
            return {
                "valid": [
                    _config_item("alpha", "org/alpha"),
                    _config_item("beta", "org/beta"),
                ],
                "invalid": [],
            }
        if method == "preview":
            name = str(params["name"])
            metadata: dict[str, object] = {
                "model_display_name": name,
                "model_ref": f"{name}-pin",
            }
            if params.get("model_ref") == "qwen-pin":
                metadata.update(
                    {
                        "model_display_name": "qwen-once",
                        "model_ref": "qwen-pin",
                        "model_revision": "sha-once",
                        "model_cache_state": "cached",
                    }
                )
            return {
                "preview": f"vllm serve {params.get('model_ref') or name}",
                "warnings": [],
                "metadata": metadata,
            }
        if method == "preflight":
            return {"ok": True, "failures": []}
        if method == "prepare_launch":
            name = str(params["name"])
            return {
                "config": {"name": name, "model": f"org/{name}"},
                "build": {
                    "argv": ["/bin/echo", "ready"],
                    "env": {},
                    "cwd": "/tmp",
                    "warnings": [],
                    "metadata": {},
                    "preview": "",
                },
            }
        if method == "launch":
            self.launch_count += 1
            return {"run_id": f"run-{self.launch_count}"}
        if method == "discover_runs":
            return {"runs": []}
        if method in {"gpu", "sample_gpus"}:
            return {"samples": [], "note": "unavailable", "unavailable": True}
        raise AssertionError(f"unexpected call: {method} {params}")

    def subscribe(self, *_args, **_kwargs):
        raise AssertionError("profile-fidelity launch test should not subscribe")


def _config_item(name: str, model: str) -> dict[str, object]:
    return {
        "path": f"/agent/configs/{name}.yaml",
        "name": name,
        "model": model,
        "target": "local",
        "warnings": [],
        "config": {"name": name, "target": "local", "model": model},
    }


async def _wait_for(condition, message: str) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(message)


def _call_params(
    client: _LaunchClient, method: str
) -> list[dict[str, object]]:
    return [params for called_method, params in client.calls if called_method == method]


def test_model_manager_names_the_transient_action_use_once() -> None:
    assert ("⏎", "Use once") in _FOOTER_HINTS
    payload = _model_selection_payload(
        {
            "entry_id": "qwen-pin",
            "display_name": "qwen-once",
            "commit_sha": "sha-once",
        }
    )
    assert payload == {
        "action": "use_model_once",
        "model_ref": "qwen-pin",
        "label": "qwen-once",
        "revision": "sha-once",
    }


@pytest.mark.asyncio
async def test_model_use_once_is_visible_and_never_leaks_across_config_switch(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _LaunchClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_client=client,
        target_ping_interval_seconds=None,
    )
    notifications: list[str] = []

    async with app.run_test() as pilot:
        await _wait_for(lambda: app.current_config is not None, "initial config did not load")
        monkeypatch.setattr(
            app,
            "notify",
            lambda message, *args, **kwargs: notifications.append(str(message)),
        )

        app._handle_model_manager_selection(
            {
                "action": "use_model_once",
                "model_ref": "qwen-pin",
                "label": "qwen-once",
                "revision": "sha-once",
                "cache_state": "cached",
            }
        )
        await _wait_for(
            lambda: app.selected_config_metadata.get("model_override_mode") == "use_once",
            "one-shot model was not visible in metadata",
        )
        assert app.selected_config_metadata["model_override_config"] == "alpha"
        assert "1×qwen-once" in app._render_active_model_segment()
        assert notifications[-1] == "Use once for alpha: qwen-once @ sha-once"

        app.select_config("beta")
        await _wait_for(
            lambda: bool(_call_params(client, "preview"))
            and _call_params(client, "preview")[-1].get("name") == "beta",
            "beta preview did not refresh",
        )
        beta_preview = _call_params(client, "preview")[-1]
        assert "model_ref" not in beta_preview
        assert "revision" not in beta_preview
        assert "model_override_mode" not in app.selected_config_metadata
        assert "1×" not in app._render_active_model_segment()
        await pilot.pause()


@pytest.mark.asyncio
async def test_model_use_once_is_consumed_once_but_shared_by_entire_launch_attempt(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _LaunchClient()
    app = VelaApp(
        configs_dir=config_dir,
        target_client=client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test():
        await _wait_for(lambda: app.current_config is not None, "initial config did not load")
        monkeypatch.setattr(app, "notify", lambda *_args, **_kwargs: None)
        app._handle_model_manager_selection(
            {
                "action": "use_model_once",
                "model_ref": "qwen-pin",
                "label": "qwen-once",
                "revision": "sha-once",
                "cache_state": "cached",
            }
        )
        await _wait_for(
            lambda: app.selected_config_metadata.get("model_override_mode") == "use_once"
            and app.selected_config_preview == "vllm serve qwen-pin",
            "one-shot model was not staged",
        )
        client.calls.clear()
        monitor = AsyncMock()
        monkeypatch.setattr(app, "_monitor_attached_run", monitor)

        await app._run_selected_config()

        for method in ("preflight", "prepare_launch", "launch"):
            params = _call_params(client, method)
            assert len(params) == 1
            assert params[0]["model_ref"] == "qwen-pin"
            assert params[0]["revision"] == "sha-once"
        assert "model_override_mode" not in app.selected_config_metadata
        assert app.selected_config_preview == "vllm serve alpha"

        client.calls.clear()
        await app._run_selected_config()

        for method in ("preflight", "prepare_launch", "launch"):
            params = _call_params(client, method)
            assert len(params) == 1
            assert "model_ref" not in params[0]
            assert "revision" not in params[0]


@pytest.mark.asyncio
async def test_real_agent_model_use_once_restores_saved_profile_after_launch(
    config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    """The effective run config must not replace the selected saved profile."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    registry_path = tmp_path / "models" / "registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "entry_id": "01REPLACEMENT",
                        "display_name": "replacement-pin",
                        "source": "hf_repo",
                        "repo_id": "org/replacement",
                        "revision": "main",
                        "commit_sha": "bbb222",
                        "cache_state": "cached",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    captured_argv = tmp_path / "captured-argv.jsonl"
    child = tmp_path / "capture-child.py"
    child.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "with open(os.environ['CAPTURE_PATH'], 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    child.chmod(0o755)
    write_yaml(
        config_dir / "alpha.yaml",
        f"""
        name: alpha
        model: org/original
        command:
          entrypoint: serve
          executable: {child}
        server:
          port: {unused_tcp_port}
        env:
          CAPTURE_PATH: {captured_argv}
        launch:
          runs_dir: {tmp_path / "runs"}
        """,
    )

    class RecordingInProcessTargetClient(InProcessTargetClient):
        def __init__(self, agent: LocalAgent) -> None:
            super().__init__(agent)
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call(
            self, method: str, params: dict[str, object] | None = None
        ) -> dict[str, object]:
            self.calls.append((method, dict(params or {})))
            return await super().call(method, params)

    client = RecordingInProcessTargetClient(
        LocalAgent(
            builds_root=tmp_path / "builds",
            models_registry_path=registry_path,
        )
    )
    app = VelaApp(
        configs_dir=config_dir,
        target_client=client,
        target_ping_interval_seconds=None,
    )

    async with app.run_test():
        await _wait_for(
            lambda: app.current_config is not None
            and app.selected_config_preview is not None,
            "saved profile did not load",
        )
        app._handle_model_manager_selection(
            {
                "action": "use_model_once",
                "model_ref": "01REPLACEMENT",
                "label": "replacement-pin",
                "revision": "bbb222",
                "cache_state": "cached",
            }
        )
        await _wait_for(
            lambda: app.selected_config_metadata.get("model_override_mode") == "use_once"
            and "org/replacement" in (app.selected_config_preview or ""),
            "one-shot preview did not resolve the selected registry model",
        )

        await app._run_selected_config()

        first_argv = [
            json.loads(line)
            for line in captured_argv.read_text(encoding="utf-8").splitlines()
        ]
        assert first_argv[0][:2] == ["serve", "org/replacement"]
        assert app.current_config is not None
        assert app.current_config.model == "org/original"
        assert app.current_config.model_ref is None
        assert app.current_config.revision is None
        assert "org/original" in (app.selected_config_preview or "")
        assert "model_override_mode" not in app.selected_config_metadata

        await app._run_selected_config()

        all_argv = [
            json.loads(line)
            for line in captured_argv.read_text(encoding="utf-8").splitlines()
        ]
        assert [argv[:2] for argv in all_argv] == [
            ["serve", "org/replacement"],
            ["serve", "org/original"],
        ]
        launch_params = [params for method, params in client.calls if method == "launch"]
        assert launch_params[0]["model_ref"] == "01REPLACEMENT"
        assert launch_params[0]["revision"] == "bbb222"
        assert "model_ref" not in launch_params[1]
        assert "revision" not in launch_params[1]


@pytest.mark.asyncio
async def test_config_picker_preselects_and_marks_current_config(tmp_path: Path) -> None:
    write_yaml(tmp_path / "alpha.yaml", "name: alpha\nmodel: org/alpha")
    write_yaml(tmp_path / "beta.yaml", "name: beta\nmodel: org/beta")
    registry = load_registry(tmp_path)
    app = _Host()

    async with app.run_test() as pilot:
        screen = ConfigPickerScreen(registry, current_config_name="beta")
        await app.push_screen(screen)
        await pilot.pause()

        assert screen.selected_index == 1
        assert "> beta  org/beta  [current]" in screen.summary
        assert "  alpha  org/alpha  [current]" not in screen.summary


def _without_regenerated_clone_fields(config: ModelConfig) -> dict[str, object]:
    payload = deepcopy(config.model_dump(mode="json"))
    payload.pop("name")
    server = payload["server"]
    assert isinstance(server, dict)
    server.pop("port")
    launch = payload["launch"]
    assert isinstance(launch, dict)
    launch.pop("runs_dir")
    command = payload["command"]
    assert isinstance(command, dict)
    docker = command.get("docker")
    if isinstance(docker, dict):
        docker.pop("container_name")
    return payload


@pytest.mark.asyncio
async def test_tui_clone_uses_agent_primitive_and_preserves_maximal_process_profile(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_yaml(
        config_dir / "process-source.yaml",
        """
        name: process-source
        target: oxcart
        description: maximal process profile
        model: org/process-model
        revision: sha-process
        model_ref: process-pin
        served_model_name: process-served
        command:
          runtime: process
          entrypoint: serve
          executable: /bin/echo
          cwd: /srv/process
        engine:
          tensor_parallel_size: 2
          pipeline_parallel_size: 1
          gpu_memory_utilization: 0.82
          max_model_len: 8192
          dtype: bfloat16
          quantization: awq
          kv_cache_dtype: fp8
          load_format: safetensors
          enforce_eager: true
          swap_space: 8
          block_size: 32
          seed: 7
          max_num_seqs: 16
        server:
          host: 0.0.0.0
          port: 18101
          exposure: lan
          probe_host: 127.0.0.1
        logging:
          request_logging: true
          suppress_access_log_for: [/health]
          max_log_len: 512
        env:
          PROFILE_MODE: audit
        extra_args: [--disable-log-stats]
        vllm:
          version_profile: current
          version: 0.10.0
          transformers_version: 4.55.0
          torch_version: 2.8.0
          cuda_version: "12.8"
          require_flags: [--max-model-len]
        launch:
          mode: detached
          ready_timeout_seconds: 321
          health:
            path: /ready
            interval_seconds: 1.5
          runs_dir: /tmp/vela-process/process-source
          require_cached_models: true
        """,
    )
    monkeypatch.setattr(local_agent_module, "_listening_ports", lambda: set())
    source = load_registry(config_dir).by_name("process-source")
    app = VelaApp(
        configs_dir=config_dir,
        target_client=InProcessTargetClient(LocalAgent()),
        target_ping_interval_seconds=None,
    )

    async with app.run_test():
        await _wait_for(lambda: app.current_config is not None, "source config did not load")
        await app._clone_deployment("process-source")
        assert not app.error_text, app.error_text
        clone = app.registry.by_name("process-source-2")

        assert _without_regenerated_clone_fields(clone) == _without_regenerated_clone_fields(
            source
        )
        assert clone.server.port != source.server.port
        assert str(clone.launch.runs_dir) == "/tmp/vela-process/process-source-2"
        assert app.current_config is not None
        assert app.current_config.name == "process-source-2"
        assert (config_dir / "process-source-2.yaml").exists()


@pytest.mark.asyncio
async def test_tui_clone_uses_agent_primitive_and_preserves_maximal_docker_profile(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_yaml(
        config_dir / "docker-source.yaml",
        """
        name: docker-source
        target: oxcart
        description: maximal docker profile
        model: org/docker-model
        revision: sha-docker
        model_ref: docker-pin
        served_model_name: docker-served
        command:
          runtime: docker
          entrypoint: serve
          cwd: /srv/docker
          docker:
            image: registry.example/vllm@sha256:abc123
            container_name: vela-docker-source
            runtime: nvidia
            gpus: device=0
            ipc_host: false
            shm_size: 24g
            network: bridge
            volumes: [/tank/models:/models:ro, /tank/work:/work]
            hf_cache: /tank/hf-cache
            hf_cache_target: /cache/hf
            env:
              NCCL_DEBUG: INFO
            restart: unless-stopped
            stop_grace_seconds: 45
            entrypoint: /usr/bin/vllm
            pull: missing
            evict: [vela-stale-a, vela-stale-b]
            extra_run_args: [--ulimit, memlock=-1:-1]
        engine:
          tensor_parallel_size: 2
          gpu_memory_utilization: 0.91
          max_model_len: 16384
          dtype: auto
          kv_cache_dtype: fp8
          max_num_seqs: 32
        server:
          host: 0.0.0.0
          port: 18102
          exposure: lan
          probe_host: 127.0.0.1
        logging:
          request_logging: false
          suppress_access_log_for: [/health, /metrics]
          max_log_len: 1024
        env:
          PROFILE_MODE: production
        extra_args: [--trust-remote-code]
        vllm:
          version_profile: current
          require_flags: [--max-model-len]
        launch:
          mode: detached
          ready_timeout_seconds: 654
          health:
            path: /health
            interval_seconds: 2.5
          runs_dir: /tmp/vela-docker/docker-source
          require_cached_models: true
        """,
    )
    monkeypatch.setattr(local_agent_module, "_listening_ports", lambda: set())
    monkeypatch.setattr(local_agent_module, "_docker_container_names", lambda: set())
    source = load_registry(config_dir).by_name("docker-source")
    app = VelaApp(
        configs_dir=config_dir,
        target_client=InProcessTargetClient(LocalAgent()),
        target_ping_interval_seconds=None,
    )

    async with app.run_test():
        await _wait_for(lambda: app.current_config is not None, "source config did not load")
        await app._clone_deployment("docker-source")
        assert not app.error_text, app.error_text
        clone = app.registry.by_name("docker-source-2")

        assert _without_regenerated_clone_fields(clone) == _without_regenerated_clone_fields(
            source
        )
        assert clone.server.port != source.server.port
        assert str(clone.launch.runs_dir) == "/tmp/vela-docker/docker-source-2"
        assert clone.command.docker is not None
        assert clone.command.docker.container_name == "vela-docker-source-2"
        assert app.current_config is not None
        assert app.current_config.name == "docker-source-2"
        assert (config_dir / "docker-source-2.yaml").exists()


@pytest.mark.asyncio
async def test_tui_clone_regenerates_owned_docker_identity_without_evicting_source(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_yaml(
        config_dir / "owned-source.yaml",
        """
        name: owned-source
        model: org/model
        command:
          runtime: docker
          entrypoint: serve
          docker:
            image: registry.example/vllm@sha256:abc123
            container_name: vela-owned-source
            evict: [vela-owned-source, deliberately-shared-cache]
            extra_run_args:
              - --label
              - ai.vela.managed=true
              - --label
              - ai.vela.profile=owned-source
              - --env
              - KEEP=this-value
        server:
          port: 18103
        launch:
          runs_dir: /tmp/vela-owned/owned-source
        """,
    )
    monkeypatch.setattr(local_agent_module, "_listening_ports", lambda: set())
    monkeypatch.setattr(local_agent_module, "_docker_container_names", lambda: set())
    app = VelaApp(
        configs_dir=config_dir,
        target_client=InProcessTargetClient(LocalAgent()),
        target_ping_interval_seconds=None,
    )

    async with app.run_test():
        await _wait_for(lambda: app.current_config is not None, "source config did not load")
        await app._clone_deployment("owned-source")
        assert not app.error_text, app.error_text
        clone = app.registry.by_name("owned-source-2")

        assert clone.command.docker is not None
        assert clone.command.docker.container_name == "vela-owned-source-2"
        assert clone.command.docker.evict == ["deliberately-shared-cache"]
        assert clone.command.docker.extra_run_args == [
            "--label",
            "ai.vela.managed=true",
            "--label",
            "ai.vela.profile=owned-source-2",
            "--env",
            "KEEP=this-value",
        ]


@pytest.mark.asyncio
async def test_saved_profile_round_trips_through_fresh_agent_and_app(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_agent_module, "_listening_ports", lambda: set())
    monkeypatch.setattr(local_agent_module, "_docker_container_names", lambda: set())
    first_client = InProcessTargetClient(LocalAgent())
    await first_client.connect()
    try:
        composed = await first_client.call(
            "compose_config",
            {
                "name": "durable-profile",
                "target": "local",
                "model": "org/model",
                "recipe": "__custom__",
                "runtime": {
                    "kind": "docker",
                    "image": "registry.example/vllm@sha256:abc123",
                },
                "configs_dir": str(config_dir),
            },
        )
        saved = await first_client.call(
            "save_config",
            {
                "name": "durable-profile",
                "config": composed["config"],
                "configs_dir": str(config_dir),
            },
        )
        first_preview = await first_client.call(
            "preview",
            {"name": "durable-profile", "configs_dir": str(config_dir)},
        )
    finally:
        await first_client.disconnect()

    second_agent = LocalAgent()
    app = VelaApp(
        configs_dir=config_dir,
        target_client=InProcessTargetClient(second_agent),
        target_ping_interval_seconds=None,
    )
    async with app.run_test():
        await _wait_for(
            lambda: app.current_config is not None,
            "fresh app did not reload saved profile",
        )
        second_preview = await app._target_call(
            "preview",
            {"name": "durable-profile", "configs_dir": str(config_dir)},
        )

    assert app.current_config is not None
    assert app.current_config.model_dump(mode="json") == saved["config"]
    assert second_preview["preview"] == first_preview["preview"]
    assert second_preview["metadata"] == first_preview["metadata"]
