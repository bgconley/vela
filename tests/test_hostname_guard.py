from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vela.agent.local import LocalAgent, TargetCallError
from vela.config.schema import ModelConfig
from vela.engine import preflight as preflight_module
from vela.engine.phases import ErrorKind
from vela.engine.preflight import check_launch_preflight


def test_launch_required_hostname_round_trips_through_schema() -> None:
    config = ModelConfig.model_validate(
        {
            "name": "host-scoped",
            "model": "org/model",
            "launch": {"required_hostname": "oxcart"},
        }
    )

    assert config.launch.required_hostname == "oxcart"
    assert config.model_dump(mode="json")["launch"]["required_hostname"] == "oxcart"


def test_preflight_rejects_host_scoped_profile_on_another_machine(monkeypatch) -> None:
    config = ModelConfig.model_validate(
        {
            "name": "oxcart-profile",
            "model": "org/model",
            "launch": {"required_hostname": "oxcart"},
        }
    )
    monkeypatch.setattr(preflight_module.platform, "node", lambda: "blackbird")

    failure = check_launch_preflight(config)

    assert failure is not None
    assert failure.kind is ErrorKind.CONFIG_INVALID
    assert failure.detail == (
        "Profile oxcart-profile requires target hostname oxcart; current hostname is "
        "blackbird. Select the intended target before launch."
    )


def test_preflight_accepts_matching_required_hostname(monkeypatch) -> None:
    config = ModelConfig.model_validate(
        {
            "name": "oxcart-profile",
            "model": "org/model",
            "launch": {"required_hostname": "oxcart"},
        }
    )
    monkeypatch.setattr(preflight_module.platform, "node", lambda: "oxcart")
    monkeypatch.setattr(preflight_module, "low_disk_space_detail", lambda *_a, **_k: None)

    assert check_launch_preflight(config) is None


def test_preflight_accepts_matching_short_name_from_fqdn(monkeypatch) -> None:
    config = ModelConfig.model_validate(
        {
            "name": "oxcart-profile",
            "model": "org/model",
            "launch": {"required_hostname": "oxcart"},
        }
    )
    monkeypatch.setattr(
        preflight_module.platform,
        "node",
        lambda: "oxcart.lab.conley.ai",
    )
    monkeypatch.setattr(preflight_module, "low_disk_space_detail", lambda *_a, **_k: None)

    assert check_launch_preflight(config) is None


def _wrong_host_process_payload() -> dict[str, object]:
    return {
        "name": "wrong-host-process",
        "model": "org/model",
        "command": {
            "runtime": "process",
            "executable": "/tmp/side-effecting-vllm",
        },
        "launch": {"required_hostname": "oxcart"},
    }


def _forbid_agent_command_resolution(
    agent: LocalAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    calls: list[str] = []

    def forbidden(label: str):
        def fail(*_args: object, **_kwargs: object) -> None:
            calls.append(label)
            raise AssertionError(f"wrong-host gate ran after {label}")

        return fail

    monkeypatch.setattr(
        agent,
        "_check_build_launch_integrity",
        forbidden("build integrity resolution"),
    )
    monkeypatch.setattr(
        agent,
        "_prepare_command_for_config",
        forbidden("executable or Docker command resolution"),
    )
    return calls


def test_agent_preflight_rejects_wrong_host_before_any_command_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight_module.platform, "node", lambda: "blackbird")
    agent = LocalAgent()
    calls = _forbid_agent_command_resolution(agent, monkeypatch)

    result = agent.handle("preflight", {"config": _wrong_host_process_payload()})

    assert isinstance(result, dict)
    assert result == {
        "ok": False,
        "failures": [
            {
                "kind": ErrorKind.CONFIG_INVALID.value,
                "detail": (
                    "Profile wrong-host-process requires target hostname oxcart; "
                    "current hostname is blackbird. Select the intended target before launch."
                ),
            }
        ],
        "warnings": [],
    }
    assert calls == []


def test_agent_prepare_launch_rejects_wrong_host_before_any_command_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight_module.platform, "node", lambda: "blackbird")
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    (configs_dir / "wrong-host-process.yaml").write_text(
        yaml.safe_dump(_wrong_host_process_payload(), sort_keys=False),
        encoding="utf-8",
    )
    agent = LocalAgent()
    calls = _forbid_agent_command_resolution(agent, monkeypatch)

    with pytest.raises(TargetCallError) as exc_info:
        agent.handle(
            "prepare_launch",
            {"name": "wrong-host-process", "configs_dir": str(configs_dir)},
        )

    assert exc_info.value.code == "preflight-failed"
    assert exc_info.value.details == {
        "kind": ErrorKind.CONFIG_INVALID.value,
        "detail": (
            "Profile wrong-host-process requires target hostname oxcart; current hostname is "
            "blackbird. Select the intended target before launch."
        ),
    }
    assert calls == []
