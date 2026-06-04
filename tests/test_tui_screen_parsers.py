from __future__ import annotations

import pytest

from vllm_loader.tui.screens.adopt_build import _parse_adopt_build_params
from vllm_loader.tui.screens.pin_model import _parse_model_pin_params


def test_adopt_build_params_allow_registry_minted_build_id() -> None:
    params = _parse_adopt_build_params(
        "label=external-nightly venv_path=/agent/venvs/vllm-nightly"
    )

    assert params == {
        "label": "external-nightly",
        "venv_path": "/agent/venvs/vllm-nightly",
    }


def test_adopt_build_params_still_require_venv_path() -> None:
    with pytest.raises(ValueError, match="venv_path"):
        _parse_adopt_build_params("label=external-nightly")


def test_pin_model_params_allow_registry_minted_entry_id() -> None:
    params = _parse_model_pin_params(
        "repo_id=sshleifer/tiny-gpt2 display_name=tiny-gpt2 revision=main"
    )

    assert params == {
        "repo_id": "sshleifer/tiny-gpt2",
        "display_name": "tiny-gpt2",
        "revision": "main",
    }


def test_pin_model_params_allow_url_source_and_quoted_notes() -> None:
    params = _parse_model_pin_params(
        "url=https://models.example/model.gguf display_name=url-model "
        "notes='operator note'"
    )

    assert params == {
        "url": "https://models.example/model.gguf",
        "display_name": "url-model",
        "notes": "operator note",
        "source": "url",
    }


def test_pin_model_params_still_require_model_source() -> None:
    with pytest.raises(
        ValueError,
        match="repo_id=<repo>, local_path=<path>, or url=<url>",
    ):
        _parse_model_pin_params("display_name=tiny-gpt2")
