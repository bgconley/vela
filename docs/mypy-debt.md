# Mypy Debt

Vela enforces mypy for new modules, but v0.1.0 accepts a bounded legacy
burn-down list. The override list in `pyproject.toml` may shrink; it should not
grow without an explicit release decision.

Current ignored modules measured with the overrides removed:

| Module | Errors |
| --- | ---: |
| `vela.cli` | 34 |
| `vela.tui.app` | 18 |
| `vela.agent.local` | 17 |
| `vela.engine.supervisor` | 14 |
| `vela.engine.model_registry` | 13 |
| `vela.engine.build_registry` | 10 |
| `vela.tui.screens.flag_manager` | 10 |
| `vela.tui.screens.new_deployment` | 8 |
| `vela.tui.screens.config_picker` | 5 |
| `vela.tui.screens.model_manager` | 3 |
| `vela.engine.process_manager` | 3 |

Total accepted-for-v0.1.0 debt: 135 errors across 11 modules.

To refresh the counts:

```bash
awk 'BEGIN{skip=0} /^\[\[tool\.mypy\.overrides\]\]/{skip=1; next} /^\[tool\.ruff\]/{skip=0} !skip{print}' \
  pyproject.toml > /tmp/vela-pyproject-no-mypy-overrides.toml
python -m mypy --config-file /tmp/vela-pyproject-no-mypy-overrides.toml src/vela
```
