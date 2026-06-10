"""Engine-level tests for build_registry venv inspection (Mac-safe; no GPU/vLLM).

``inspect_venv`` backs Adopt Build's live validation card: a fast, filesystem-only
probe (no subprocess, no imports) that reports whether a venv is adoptable and
which vllm/torch/python versions it carries.
"""

from __future__ import annotations

from pathlib import Path

from vela.engine.build_registry import inspect_venv


def _make_venv(
    root: Path,
    *,
    python: bool = True,
    vllm_bin: bool = True,
    vllm_dist: str | None = "0.11.2",
    torch_dist: str | None = "2.6.0",
    pyvenv_version: str | None = "3.12.8",
) -> Path:
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    if python:
        (venv / "bin" / "python").write_text("")
    if vllm_bin:
        (venv / "bin" / "vllm").write_text("")
    if pyvenv_version is not None:
        (venv / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion = {pyvenv_version}\n")
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    if vllm_dist is not None:
        (site / f"vllm-{vllm_dist}.dist-info").mkdir()
    if torch_dist is not None:
        (site / f"torch-{torch_dist}.dist-info").mkdir()
    return venv


def test_inspect_venv_reports_real_versions(tmp_path: Path) -> None:
    venv = _make_venv(tmp_path)
    result = inspect_venv(venv)
    assert result["ok"] is True
    assert result["vllm_version"] == "0.11.2"
    assert result["torch_version"] == "2.6.0"
    assert result["python_version"] == "3.12.8"


def test_inspect_venv_missing_path_is_not_ok(tmp_path: Path) -> None:
    result = inspect_venv(tmp_path / "nope")
    assert result["ok"] is False
    assert "exist" in result["reason"]


def test_inspect_venv_without_python_is_not_a_venv(tmp_path: Path) -> None:
    venv = _make_venv(tmp_path, python=False)
    result = inspect_venv(venv)
    assert result["ok"] is False
    assert "bin/python" in result["reason"]


def test_inspect_venv_without_vllm_executable_is_not_adoptable(tmp_path: Path) -> None:
    venv = _make_venv(tmp_path, vllm_bin=False)
    result = inspect_venv(venv)
    assert result["ok"] is False
    assert "bin/vllm" in result["reason"]


def test_inspect_venv_without_vllm_package_names_the_problem(tmp_path: Path) -> None:
    venv = _make_venv(tmp_path, vllm_dist=None)
    result = inspect_venv(venv)
    assert result["ok"] is False
    assert "vllm" in result["reason"]


def test_inspect_venv_supports_uv_style_version_info_key(tmp_path: Path) -> None:
    venv = _make_venv(tmp_path, pyvenv_version=None)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion_info = 3.13.1\n")
    result = inspect_venv(venv)
    assert result["ok"] is True
    assert result["python_version"] == "3.13.1"


def test_discover_venvs_scans_roots_and_annotates(tmp_path: Path) -> None:
    # J35: discovery lists python venvs under the roots, annotated with the
    # same fast inspection Adopt's validation uses.
    root = tmp_path / "venvs"
    root.mkdir()
    full = _make_venv(root)  # creates root/venv with vllm 0.11.2
    full.rename(root / "vllm-nightly")
    bare = root / "plain-env"
    (bare / "bin").mkdir(parents=True)
    (bare / "bin" / "python").write_text("")
    (root / "not-a-venv").mkdir()

    from vela.engine.build_registry import discover_venvs

    results = discover_venvs(roots=[root, tmp_path / "missing-root"])
    paths = {entry["venv_path"]: entry for entry in results}
    assert str(root / "vllm-nightly") in paths
    assert paths[str(root / "vllm-nightly")]["ok"] is True
    assert paths[str(root / "vllm-nightly")]["vllm_version"] == "0.11.2"
    assert str(bare / "") .rstrip("/") in {p.rstrip("/") for p in paths} or str(bare) in paths
    assert paths[str(bare)]["ok"] is False
    assert str(root / "not-a-venv") not in paths
