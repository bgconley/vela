# Final quality gates

**Result: PASS**

The final Python, source, documentation-test, and test-suite tree was checked with
Homebrew Python 3.11 on 2026-07-13. Evidence/report files added afterward do not
change executable or test behavior.

| Gate | Result |
|---|---|
| `python3 -m ruff check .` | pass, `All checks passed!` |
| `python3 -m mypy` | pass, no issues in 75 source files |
| `python3 scripts/check_mypy_overrides.py` | pass, ratchet remains 11 ignored modules |
| focused attached-run/UI-lifecycle documentation regression | 1 passed in 0.85s |
| `python3 -m pytest -q` | **1569 passed in 290.92s** |
| `git diff --check` | pass |

Post-suite residue inventory at `2026-07-13T14:00:21Z`:

- no `fake_vllm_child.py` process;
- no `vela.engine.supervisor` process;
- listeners 8765, 60821, 60867, and 50979 absent;
- shared Mac daemon PID 23734 remained untouched, had no children, and reported
  an empty live-run list;
- the daemon was the July 13 process at revision `v0.1.0-108-gde9b0a1`, not the
  claimed June 9 daemon; PID 20556 was absent during the earlier identity audit.

The shared daemon revision is older than the runtime-under-test SHA, but it was not
part of the owner-amended Oxcart controller/target proof. A safe optional restart is
performed only after the final evidence commit and an empty-run preflight.

