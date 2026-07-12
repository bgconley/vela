from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_yaml

from vela.config.targets import (
    TargetConfig,
    TransportKind,
    load_default_target,
    load_targets_file,
    remove_target_file,
    save_default_target,
    upsert_target_file,
)


def test_missing_targets_file_still_yields_implicit_local_target(tmp_path: Path) -> None:
    registry = load_targets_file(tmp_path / "targets.yaml")

    local = registry.by_name("local")
    assert [target.name for target in registry.targets] == ["local"]
    assert local.transport is TransportKind.LOCAL
    assert local.host is None


def test_targets_registry_loads_ssh_targets_with_local_first(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "targets.yaml",
        """
        targets:
          blackbird:
            transport: ssh
            host: bgconley@10.25.0.51
            ssh_key: /home/bgconley/.ssh/vela_ed25519
            agent_command:
              - /home/bgconley/venvs/current-vela/bin/vela
              - agent
              - connect
            workdir: /tank/repos/vela
            venv: /tank/venvs/vela
            ssh_opts_env: VELA_SSH_OPTS
        """,
    )

    registry = load_targets_file(path)
    blackbird = registry.by_name("blackbird")

    assert [target.name for target in registry.targets] == ["local", "blackbird"]
    assert blackbird.transport is TransportKind.SSH
    assert blackbird.host == "bgconley@10.25.0.51"
    assert blackbird.ssh_key == Path("/home/bgconley/.ssh/vela_ed25519")
    assert blackbird.agent_command == [
        "/home/bgconley/venvs/current-vela/bin/vela",
        "agent",
        "connect",
    ]
    assert blackbird.workdir == Path("/tank/repos/vela")
    assert blackbird.venv == Path("/tank/venvs/vela")
    assert blackbird.ssh_opts_env == "VELA_SSH_OPTS"


def test_targets_registry_loads_json_targets_file(tmp_path: Path) -> None:
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps(
            {
                "targets": {
                    "blackbird": {
                        "transport": "ssh",
                        "host": "bgconley@10.25.0.51",
                        "workdir": "/tank/repos/vela",
                        "venv": "/tank/venvs/vela",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    registry = load_targets_file(path)
    blackbird = registry.by_name("blackbird")

    assert [target.name for target in registry.targets] == ["local", "blackbird"]
    assert blackbird.transport is TransportKind.SSH
    assert blackbird.host == "bgconley@10.25.0.51"
    assert blackbird.workdir == Path("/tank/repos/vela")
    assert blackbird.venv == Path("/tank/venvs/vela")


def test_default_targets_path_falls_back_to_existing_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "vela" / "targets.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "targets": {
                    "blackbird": {
                        "transport": "ssh",
                        "host": "bgconley@10.25.0.51",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    registry = load_targets_file()

    assert [target.name for target in registry.targets] == ["local", "blackbird"]
    assert registry.by_name("blackbird").host == "bgconley@10.25.0.51"


def test_default_upsert_preserves_existing_json_targets_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "vela" / "targets.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"targets": {}}), encoding="utf-8")

    written = upsert_target_file(
        TargetConfig(
            name="blackbird",
            transport=TransportKind.SSH,
            host="bgconley@10.25.0.51",
        )
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert written == path
    assert payload["targets"]["blackbird"]["transport"] == "ssh"
    assert payload["targets"]["blackbird"]["host"] == "bgconley@10.25.0.51"
    assert not (tmp_path / "vela" / "targets.yaml").exists()


def test_targets_registry_does_not_allow_overriding_implicit_local(
    tmp_path: Path,
) -> None:
    path = write_yaml(
        tmp_path / "targets.yaml",
        """
        targets:
          local:
            transport: ssh
            host: wrong-host
          blackbird:
            transport: ssh
            host: bgconley@10.25.0.51
        """,
    )

    registry = load_targets_file(path)

    assert [target.name for target in registry.targets] == ["local", "blackbird"]
    assert registry.by_name("local").transport is TransportKind.LOCAL
    assert registry.by_name("local").host is None


def test_ssh_target_requires_host(tmp_path: Path) -> None:
    path = write_yaml(
        tmp_path / "targets.yaml",
        """
        targets:
          blackbird:
            transport: ssh
        """,
    )

    with pytest.raises(ValueError, match="blackbird.*host"):
        load_targets_file(path)


def test_targets_registry_does_not_remove_implicit_local(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="local.*implicit"):
        remove_target_file("local", tmp_path / "targets.yaml")


def test_default_target_round_trips_and_preserves_targets(tmp_path: Path) -> None:
    # 7.3: the persisted default_target survives a round-trip, sits alongside the
    # targets it names, and can be cleared without dropping the targets.
    path = tmp_path / "targets.yaml"
    upsert_target_file(
        TargetConfig(name="blackbird", transport=TransportKind.SSH, host="u@h"), path
    )
    assert load_default_target(path) is None

    save_default_target("blackbird", path)
    assert load_default_target(path) == "blackbird"
    assert [target.name for target in load_targets_file(path).targets] == ["local", "blackbird"]

    save_default_target(None, path)
    assert load_default_target(path) is None
    assert [target.name for target in load_targets_file(path).targets] == ["local", "blackbird"]


def test_saving_a_target_preserves_the_existing_default(tmp_path: Path) -> None:
    # Adding another target must not wipe a previously-saved default.
    path = tmp_path / "targets.yaml"
    upsert_target_file(
        TargetConfig(name="blackbird", transport=TransportKind.SSH, host="u@h"), path
    )
    save_default_target("blackbird", path)

    upsert_target_file(
        TargetConfig(name="thunderbird", transport=TransportKind.SSH, host="u@h2"), path
    )

    assert load_default_target(path) == "blackbird"
