from __future__ import annotations

from vela.tui.screens import adopt_build, create_build, pin_model


def test_structured_tui_forms_do_not_export_legacy_free_form_parsers() -> None:
    assert not hasattr(create_build, "_parse_build_params")
    assert not hasattr(adopt_build, "_parse_adopt_build_params")
    assert not hasattr(pin_model, "_parse_model_pin_params")
