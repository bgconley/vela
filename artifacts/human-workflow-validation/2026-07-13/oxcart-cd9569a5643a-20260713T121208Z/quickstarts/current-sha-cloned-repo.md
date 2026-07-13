# Current-SHA cloned-repository quickstart

- Result: **PASS**
- Completed: `2026-07-13T13:50:44Z`
- Tested revision: `cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7`
- Branch cloned: `remediate/2026-07-09-review`, then detached at the exact revision
- Clone root: `/private/tmp/vela-clone-cd9569a-final/source`
- Fresh virtual environment: `/private/tmp/vela-clone-cd9569a-final/venv`
- Python: Homebrew Python 3.11

From the clean clone root, the README path was executed as written:

```text
python3 -m venv <fresh-venv>
<fresh-venv>/bin/python -m pip install -e ".[dev]"
vela list
vela run fake-child --preview
vela smoke fake-child
```

Observed results:

- editable import resolved to the clean clone's `src/vela/__init__.py`;
- `vela --version` returned `0.1.0`;
- `vela list` included `fake-child` from the clone's `./configs` directory;
- preview resolved the repository script, model `fake/model`, served id
  `fake-model`, and `127.0.0.1:8765` without launching compute;
- smoke reached `READY http://127.0.0.1:8765 models=fake-model`, then stopped;
- `vela runs list --target local --json` returned `{"runs": []}`;
- the isolated daemon reported revision `v0.1.0-112-gcd9569a`, PID `44429`, and
  its isolated socket before explicit stop;
- the post-stop daemon status was `not-running`;
- the clone remained Git-clean after install and execution.

Postflight separately proved PID `44429`, the fake child, the smoke supervisor, and
listener `8765` absent. The exact isolated root was then removed and its absence was
verified. The user's shared daemon and state were not used or modified.

