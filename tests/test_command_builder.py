from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from vela.config.schema import ModelConfig
from vela.engine.command_builder import (
    build_command,
    is_local_model_reference,
    mask_preview_value,
    render_standalone_docker_script,
)
from vela.engine.profile import bundled_profile


def cfg(data: dict) -> ModelConfig:
    base = {"name": "llama", "model": "org/model"}
    base.update(data)
    return ModelConfig.model_validate(base)


def test_exact_argv_env_for_serve_entrypoint() -> None:
    model_cfg = cfg(
        {
            "served_model_name": "served",
            "command": {"entrypoint": "serve"},
            "engine": {"tensor_parallel_size": 2, "gpu_memory_utilization": 0.9, "dtype": "auto"},
            "server": {"host": "127.0.0.1", "port": 8001, "api_key": "sk-live"},
            "env": {"CUDA_VISIBLE_DEVICES": "0,1", "HF_TOKEN": "hf_live"},
        }
    )

    result = build_command(model_cfg, bundled_profile("current"))

    assert result.argv == [
        "vllm",
        "serve",
        "org/model",
        "--served-model-name",
        "served",
        "--host",
        "127.0.0.1",
        "--port",
        "8001",
        "--tensor-parallel-size",
        "2",
        "--gpu-memory-utilization",
        "0.9",
        "--dtype",
        "auto",
    ]
    assert result.env == {
        "PYTHONUNBUFFERED": "1",
        "CUDA_VISIBLE_DEVICES": "0,1",
        "HF_TOKEN": "hf_live",
        "VLLM_API_KEY": "sk-live",
    }
    assert "sk-live" not in result.preview
    assert "hf_live" not in result.preview


def test_revision_pin_is_emitted_for_standalone_model_handoff() -> None:
    model_cfg = cfg({"revision": "abc123"})

    result = build_command(model_cfg, bundled_profile("current"))

    assert result.argv[:5] == ["vllm", "serve", "org/model", "--revision", "abc123"]


def test_command_cwd_controls_preview_and_result(tmp_path: Path) -> None:
    work_dir = tmp_path / "serve-root"
    model_cfg = cfg({"command": {"cwd": str(work_dir)}})

    result = build_command(model_cfg, bundled_profile("current"))

    assert result.cwd == work_dir
    assert result.preview.startswith(f"cwd={work_dir}\n")


def test_exact_argv_env_for_module_entrypoint() -> None:
    model_cfg = cfg({"command": {"entrypoint": "module"}})

    result = build_command(model_cfg, bundled_profile("current"))

    assert result.argv[:5] == [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        "org/model",
    ]


def test_command_executable_override_for_both_entrypoints() -> None:
    serve = build_command(cfg({"command": {"entrypoint": "serve", "executable": "uvx"}}))
    module = build_command(cfg({"command": {"entrypoint": "module", "executable": "/opt/python"}}))

    assert serve.argv[:3] == ["uvx", "serve", "org/model"]
    assert module.argv[:5] == [
        "/opt/python",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        "org/model",
    ]


def test_boolean_flag_elision() -> None:
    model_cfg = cfg({"engine": {"enforce_eager": False}})

    result = build_command(model_cfg, bundled_profile("current"))

    assert "--enforce-eager" not in result.argv


def test_request_logging_policy_matrix() -> None:
    current = bundled_profile("current")
    older = bundled_profile("older-request-logging-on")
    unknown = bundled_profile("unknown-default")
    missing = bundled_profile("unknown-default").without_flags(
        "enable_request_logging", "disable_request_logging"
    )

    assert "--disable-log-requests" not in build_command(cfg({}), current).argv
    assert "--disable-log-requests" in build_command(cfg({}), older).argv
    assert (
        "--enable-log-requests"
        in build_command(cfg({"logging": {"request_logging": True}}), current).argv
    )
    assert "--disable-log-requests" in build_command(cfg({}), unknown).argv
    missing_result = build_command(cfg({}), missing)
    assert "--disable-log-requests" not in missing_result.argv
    assert any("request logging" in warning.lower() for warning in missing_result.warnings)


def test_select_profile_hard_gates_require_flags_against_collected_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vela.engine import profile as profile_module

    monkeypatch.setattr(
        profile_module,
        "detect_vllm_version",
        lambda executable="vllm": "0.11.2",
    )
    monkeypatch.setattr(
        profile_module,
        "collect_serve_help",
        lambda executable="vllm": "usage: vllm serve\n  --host TEXT\n  --port INTEGER\n",
    )
    selected = profile_module.select_profile()
    model_cfg = cfg({"vllm": {"require_flags": ["--disable-log-requests"]}})

    with pytest.raises(profile_module.VllmProfileError, match="--disable-log-requests"):
        selected.soft_validate(model_cfg)


def test_select_profile_ignores_summary_only_help_that_omits_serve_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vela.engine import profile as profile_module

    monkeypatch.setattr(
        profile_module,
        "detect_vllm_version",
        lambda executable="vllm": "0.19.1",
    )
    monkeypatch.setattr(
        profile_module,
        "collect_serve_help",
        lambda executable="vllm": "usage: vllm serve [-h]\noptions:\n  -h, --help\n",
    )

    selected = profile_module.select_profile("0.11", executable="vllm")
    result = build_command(cfg({"server": {"port": 8017}}), selected)

    assert "--host" in result.argv
    assert "--port" in result.argv
    assert "--disable-log-requests" in selected.known_flags


def test_profile_detection_refreshes_when_executable_path_changes(tmp_path: Path) -> None:
    from vela.engine import profile as profile_module

    executable = tmp_path / "vllm"

    def clear_caches() -> None:
        clear_profile_caches = getattr(profile_module, "clear_profile_caches", None)
        if callable(clear_profile_caches):
            clear_profile_caches()
            return
        for name in ("detect_vllm_version", "collect_serve_help"):
            cache_clear = getattr(getattr(profile_module, name), "cache_clear", None)
            if callable(cache_clear):
                cache_clear()

    def write_fake_vllm(version: str, help_text: str) -> None:
        executable.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import sys",
                    f"version = {version!r}",
                    f"help_text = {help_text!r}",
                    "if sys.argv[1:] == ['--version']:",
                    "    print(f'vllm version {version}')",
                    "elif sys.argv[1:] in (['serve', '--help'], ['serve', '--help=all']):",
                    "    print(help_text)",
                    "else:",
                    "    print('unexpected args', sys.argv[1:])",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        now = time.time()
        os.utime(executable, (now, now))

    clear_caches()
    write_fake_vllm("0.9.0", "--host TEXT")
    assert profile_module.detect_vllm_version(str(executable)) == "0.9.0"
    assert profile_module.collect_serve_help(str(executable)) == "--host TEXT\n"

    write_fake_vllm("0.11.0", "--host TEXT\n--kv-cache-dtype TEXT")
    assert profile_module.detect_vllm_version(str(executable)) == "0.11.0"
    assert profile_module.collect_serve_help(str(executable)) == (
        "--host TEXT\n--kv-cache-dtype TEXT\n"
    )


def test_targeted_access_log_suppression_only_when_profile_supports_it() -> None:
    model_cfg = cfg({"logging": {"suppress_access_log_for": ["/health", "/metrics"]}})
    current = build_command(model_cfg, bundled_profile("current"))
    unsupported = build_command(
        model_cfg, bundled_profile("current").without_flags("disable_access_log_for_endpoints")
    )

    assert current.argv[-2:] == ["--disable-access-log-for-endpoints", "/health,/metrics"]
    assert "--disable-access-log-for-endpoints" not in unsupported.argv
    assert unsupported.warnings


def test_max_log_len_and_extra_args_are_appended_verbatim() -> None:
    model_cfg = cfg({"logging": {"max_log_len": 32}, "extra_args": ["--x", "1"]})

    result = build_command(model_cfg, bundled_profile("current"))

    assert result.argv[-4:] == ["--max-log-len", "32", "--x", "1"]


def test_docker_runtime_generates_masked_docker_run_without_double_serve(
    tmp_path: Path,
) -> None:
    model_cfg = cfg(
        {
            "model": "Qwen/Qwen3-32B",
            "served_model_name": "qwen3",
            "command": {
                "runtime": "docker",
                "docker": {
                    "image": "vllm/vllm-openai@sha256:abc",
                    "container_name": "vela-qwen3",
                    "hf_cache": str(tmp_path / "hf-cache"),
                    "env": {"HF_HOME": "/root/.cache/huggingface"},
                    "extra_run_args": ["--ulimit", "nofile=1048576:1048576"],
                },
            },
            "engine": {
                "tensor_parallel_size": 2,
                "gpu_memory_utilization": 0.95,
                "dtype": "auto",
            },
            "server": {
                "host": "0.0.0.0",
                "port": 18003,
                "exposure": "lan",
                "api_key": "sk-live",
            },
            "env": {"HF_TOKEN": "hf_live"},
            "extra_args": ["--kv-cache-memory-bytes", "64424509440"],
        }
    )

    result = build_command(model_cfg, bundled_profile("current"), cwd=tmp_path)

    assert result.argv[:4] == ["docker", "run", "-d", "--name"]
    assert "vela-qwen3" in result.argv
    assert ["--gpus", "all"] == result.argv[
        result.argv.index("--gpus") : result.argv.index("--gpus") + 2
    ]
    assert "--ipc=host" in result.argv
    assert ["--shm-size", "32g"] == result.argv[
        result.argv.index("--shm-size") : result.argv.index("--shm-size") + 2
    ]
    assert ["--network", "host"] == result.argv[
        result.argv.index("--network") : result.argv.index("--network") + 2
    ]
    assert ["--restart", "no"] == result.argv[
        result.argv.index("--restart") : result.argv.index("--restart") + 2
    ]
    assert ["-e", "HF_TOKEN"] == result.argv[
        result.argv.index("HF_TOKEN") - 1 : result.argv.index("HF_TOKEN") + 1
    ]
    assert ["-e", "VLLM_API_KEY"] == result.argv[
        result.argv.index("VLLM_API_KEY") - 1 : result.argv.index("VLLM_API_KEY") + 1
    ]
    image_index = result.argv.index("vllm/vllm-openai@sha256:abc")
    assert result.argv[image_index + 1] == "Qwen/Qwen3-32B"
    assert "serve" not in result.argv[image_index + 1 :]
    assert "--kv-cache-memory-bytes" in result.argv[image_index + 1 :]
    assert result.env["HF_TOKEN"] == "hf_live"
    assert result.env["VLLM_API_KEY"] == "sk-live"
    assert "hf_live" not in result.preview
    assert "sk-live" not in result.preview
    assert result.metadata["runtime"] == "docker"


def test_bf16_docker_runtime_does_not_inherit_fp8_kv_pin() -> None:
    model_cfg = cfg(
        {
            "model": "Qwen/Qwen3-32B",
            "command": {
                "runtime": "docker",
                "docker": {"image": "vllm/vllm-openai@sha256:abc"},
            },
            "engine": {"dtype": "bfloat16", "gpu_memory_utilization": 0.95},
            "server": {"port": 18002},
        }
    )

    result = build_command(model_cfg, bundled_profile("current"))

    assert "--kv-cache-memory-bytes" not in result.argv
    assert "serve" not in result.argv[result.argv.index("vllm/vllm-openai@sha256:abc") + 1 :]


def test_standalone_docker_export_redacts_secrets_and_reproduces_run(tmp_path: Path) -> None:
    model_cfg = cfg(
        {
            "model": "Qwen/Qwen3.6-27B-FP8",
            "served_model_name": "qwen36",
            "command": {
                "runtime": "docker",
                "docker": {
                    "image": "vllm/vllm-openai@sha256:abc",
                    "container_name": "vela-qwen36",
                    "hf_cache": str(tmp_path / "hf-cache"),
                    "env": {"FLASHINFER_CUDA_ARCH_LIST": "12.0f"},
                },
            },
            "server": {
                "host": "0.0.0.0",
                "port": 18003,
                "exposure": "lan",
                "api_key": "EMPTY",
            },
            "env": {"HF_TOKEN": "hf_live_token"},
            "extra_args": [
                "--attention-backend",
                "FLASHINFER",
                "--header",
                "Authorization: Bearer sk-header-secret",
            ],
        }
    )

    result = build_command(model_cfg, bundled_profile("current"), cwd=tmp_path)
    script = render_standalone_docker_script(result, name=model_cfg.name)

    assert script.startswith("#!/usr/bin/env bash\n")
    assert f"cd {tmp_path}" in script
    assert "docker run" in script
    assert "vllm/vllm-openai@sha256:abc" in script
    assert "Qwen/Qwen3.6-27B-FP8" in script
    assert "--attention-backend" in script
    assert "FLASHINFER" in script
    assert "FLASHINFER_CUDA_ARCH_LIST=12.0f" in script
    assert ': "${HF_TOKEN:?Set HF_TOKEN before running}"' in script
    assert "hf_live_token" not in script
    assert "sk-header-secret" not in script
    assert "Authorization: Bearer REDACTED" in script
    image_index = script.index("vllm/vllm-openai@sha256:abc")
    assert "vllm/vllm-openai@sha256:abc \\\n  serve" not in script[image_index:]


def test_model_reference_local_vs_hf_repo_logic(tmp_path: Path) -> None:
    existing = tmp_path / "relative-model"
    existing.mkdir()

    assert is_local_model_reference("/abs/model")
    assert is_local_model_reference("./model")
    assert is_local_model_reference("../model")
    assert is_local_model_reference("~/model")
    assert is_local_model_reference("relative-model", cwd=tmp_path)
    assert not is_local_model_reference("org/model", cwd=tmp_path)


def test_secret_masking_in_preview() -> None:
    assert mask_preview_value("HF_TOKEN", "hf_secret") == "••••"
    assert mask_preview_value("VLLM_API_KEY", "sk-secret") == "••••"
    assert mask_preview_value("CUDA_VISIBLE_DEVICES", "0") == "0"


def test_secret_like_argv_values_are_masked_in_preview() -> None:
    model_cfg = cfg(
        {
            "extra_args": [
                "--api-key",
                "sk-preview-secret",
                "--header",
                "Authorization: Bearer preview-bearer",
                "--hf-token-copy",
                "hf_preview_secret",
            ]
        }
    )

    result = build_command(model_cfg, bundled_profile("current"))

    assert "sk-preview-secret" in result.argv
    assert "Authorization: Bearer preview-bearer" in result.argv
    assert "hf_preview_secret" in result.argv
    assert "sk-preview-secret" not in result.preview
    assert "preview-bearer" not in result.preview
    assert "hf_preview_secret" not in result.preview
    assert "Authorization: Bearer ••••" in result.preview
    assert "••••" in result.preview
