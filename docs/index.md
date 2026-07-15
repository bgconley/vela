# Vela documentation

Vela is a terminal application for composing, launching, and operating vLLM
servers. A controller runs the TUI or CLI; a target-side agent owns the files,
models, builds, GPUs, containers, processes, health probes, and durable logs on
the machine that does the work.

![A real Vela deployment at READY with its selected identity, endpoint, phase timeline, GPU status, and scrubbed logs](img/tutorial/run-ready.jpg)

## Start here

- **Installing Vela for the first time:** follow [Getting started](getting-started.md).
- **Creating and proving a real deployment:** follow the illustrated
  [first deployment tutorial](tutorials/first-deployment.md).
- **Trying Vela without a GPU:** use the repository's `fake-child` deployment in
  the [local no-GPU walkthrough](getting-started.md#try-the-no-gpu-demo-from-a-clone).
- **Connecting a GPU host:** bootstrap it from the controller in
  [Local and remote targets](getting-started.md#local-and-remote-targets).
- **Learning the mental model:** read [Core concepts](concepts.md) before designing
  production profiles or a multi-host topology.

## Operator guides

| Guide | Use it when you want to… |
| --- | --- |
| [Getting started](getting-started.md) | Install, diagnose, open the TUI, or run the no-GPU demo. |
| [First deployment tutorial](tutorials/first-deployment.md) | Create, review, save, cold-reload, launch, verify, and stop a real profile step by step. |
| [Operations guide](operations.md) | Run day-two target, profile, launch, log, model, build, flag, daemon, and retention workflows. |
| [Deployments](deployments.md) | Understand immutable deployment identity, recipes, Review, Save, and Save & Smoke. |
| [Configuration](configuration.md) | Choose targets, discover configs, understand YAML fields, and control paths or precedence. |
| [Builds and models](builds-and-models.md) | Install or adopt a vLLM build, pin or download a model, and understand cache verification. |
| [Docker runtime](docker-runtime.md) | Configure a digest-pinned container, cache mounts, pull policy, lifecycle, and export. |
| [Troubleshooting](troubleshooting.md) | Turn a named Vela error into the exact corrective command. |

## Reference

- [CLI reference](cli-reference.md) documents the complete visible command tree
  and each command's options.
- [Environment variables and storage paths](environment.md) separates
  controller, target-agent, workload, XDG, Hugging Face, SSH, and maintainer-only
  settings.
- [TUI key reference](tui.md) is generated directly from the application's
  bindings and is the authority for screen-level shortcuts.
- [Agent RPC](agent-rpc.md) documents controller/agent authority, transports,
  lifecycle methods, events, and authentication.
- [Core concepts](concepts.md) explains targets, configs, builds, model pins,
  runtime identity, runs, jobs, and the safety boundary between controller and
  agent.

## Maintainer and release material

These pages describe validation infrastructure and repository maintenance. They
are not prerequisites for normal Vela use.

- [Maintainer lab GPU workflow](gpu-workflow.md)
- [Oxcart-local visible release validation](oxcart-local-validation.md)
- [Mypy debt and ratchet](mypy-debt.md)
- [Current implementation plans](plans/)
- [Living design specifications](specs/)
- [Archived reviews, sessions, and plans](history/)

## Documentation conventions

- Commands use `gpu-node` as a replaceable target name and `user@host` as a
  replaceable SSH destination.
- `local` always means the machine running the selected Vela agent. When the
  controller itself runs on a GPU server, `local` is that server—not the laptop
  from which you opened an SSH session.
- Examples default to loopback exposure. Treat LAN/public binds, browser-served
  TUI sessions, model API keys, and agent tokens as security-sensitive.
- Screenshots are byte-for-byte copies of a checksummed live Oxcart workflow,
  not fabricated mockups. Values shown in them are host-specific examples; your
  target, model, digest, revision, port, and paths will differ. See the
  [screenshot manifest](img/tutorial/manifest.json) for source paths and hashes.
