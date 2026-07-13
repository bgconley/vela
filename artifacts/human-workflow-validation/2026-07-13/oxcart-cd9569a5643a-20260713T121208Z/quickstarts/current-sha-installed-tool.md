# Current-SHA installed-tool quickstart

- Result: **PASS**
- Tested revision: `cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7`
- Install authority: `uv tool install git+https://github.com/bgconley/vela.git@cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7`
- Isolation root: `/private/tmp/vela-quickstart-installed-cd9569a-final`
- Controller topology: isolated local Mac process and state; no shared daemon or user configuration was used

The tool was installed from the exact published commit into isolated `HOME`, XDG,
and uv tool/cache directories. The imported `vela` package resolved inside the
isolated uv tool environment and outside the working repository. `vela --version`
and the documented help surface succeeded.

A real PTY first-run of `vela` rendered all three required empty-state/guidance
strings:

- `Vela ready`
- `Quick start`
- `No configs yet`

Sending `q` closed the TUI with exit status 0. `vela list` then returned the honest
empty installed-tool result because an installed wheel does not ship the repository's
development `./configs` directory.

The isolated daemon created during the check was PID `41279`; it was stopped through
its explicit isolated socket, its identity was absent afterward, and the isolation
root was removed. No shared Mac daemon was signaled.

