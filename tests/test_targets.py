from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_yaml

from vllm_loader.config.targets import TransportKind, load_targets_file, remove_target_file


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
            workdir: /tank/repos/lab-tui
            venv: /tank/venvs/lab-tui
            ssh_opts_env: VLLM_LOADER_SSH_OPTS
        """,
    )

    registry = load_targets_file(path)
    blackbird = registry.by_name("blackbird")

    assert [target.name for target in registry.targets] == ["local", "blackbird"]
    assert blackbird.transport is TransportKind.SSH
    assert blackbird.host == "bgconley@10.25.0.51"
    assert blackbird.workdir == Path("/tank/repos/lab-tui")
    assert blackbird.venv == Path("/tank/venvs/lab-tui")
    assert blackbird.ssh_opts_env == "VLLM_LOADER_SSH_OPTS"


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
