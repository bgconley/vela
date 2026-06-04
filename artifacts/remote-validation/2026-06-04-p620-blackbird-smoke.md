# P620 to Blackbird Remote Smoke - 2026-06-04

Commit validated: `11cf9c7`

Controller: `620-01` (`bgconley@10.25.0.50`)

Agent target: `blackbird` (`bgconley@10.25.0.51`)

SSH note: the controller path used SSH agent forwarding from the Mac with the
same ED25519 key. P620's `blackbird` target does not currently set
`ssh_opts_env`; without forwarding, plain P620 to Blackbird SSH failed with
`Permission denied (publickey,password)`.

## Target Handshake

Command:

```bash
ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 \
  -o BatchMode=yes bgconley@10.25.0.50 \
  'cd /home/bgconley/repos/lab-tui &&
   /home/bgconley/venvs/lab-tui/bin/vllm-loader targets test blackbird'
```

Output:

```text
blackbird	ok	agent=0.1.0	protocol=1
```

## Agent-Owned Views

P620 listed Blackbird configs through the `blackbird` target:

```text
fake-child	fake/model
qwen3-32b-fp8-62001	/tank/trt/models/Qwen3-32B-FP8
qwen36-27b-fp8-kvfp8-rp6000-blackbird	Qwen/Qwen3.6-27B-FP8
real-vllm-example	mistralai/Mistral-7B-Instruct-v0.3
```

P620 listed Blackbird managed builds through the same target:

```text
01KT8MSSEW01E4GHGK8S251EQG	remote-smoke-vllm-0-11-0-20260604	failed
01KT8N5GN4PZFZAKV6HY1B96V1	remote-smoke-vllm-0-11-0-20260604b	creating
01KT8NPX5NPQMZDN4PQYRN9PHN	remote-smoke-vllm-0-11-0-20260604c	broken
01KT8PCCCAVC9MRPRT2444YG81	remote-smoke-vllm-0-11-0-20260604d	ready
01KT8V0TZ4655TH2PBGC0HEJEZ	p4-real-vllm-0112-20260604081158	ready
01KT8YAS0E21M8WSHGD8E8FPSW	remote-smoke-vllm-0112-20260604	ready
```

P620 listed Blackbird managed models through the same target:

```text
remote-smoke-tiny-gpt2-20260604c	remote-smoke-tiny-gpt2-20260604c	hf_repo	partial
remote-smoke-tiny-gpt2-20260604d	remote-smoke-tiny-gpt2-20260604d	hf_repo	cached
01KT8TV87R7N1YJSJA80YDMTZN	p4-remote-tiny-llama	hf_repo	cached
remote-smoke-tiny-gpt2-20260604b	remote-smoke-tiny-gpt2-20260604b	hf_repo	cached
```

Preview of `qwen36-27b-fp8-kvfp8-rp6000-blackbird` was produced on Blackbird
and returned to P620. The preview scrubbed `VLLM_API_KEY` as `••••` and warned
about `0.0.0.0` exposure.

## Real Launch Smoke

Command:

```bash
ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 \
  -o BatchMode=yes bgconley@10.25.0.50 \
  'cd /home/bgconley/repos/lab-tui &&
   timeout 2700 /home/bgconley/venvs/lab-tui/bin/vllm-loader smoke-tui \
     qwen36-27b-fp8-kvfp8-rp6000-blackbird --target blackbird'
```

Output:

```text
READY http://10.25.0.51:18003 models=qwen36-27b-fp8-kvfp8-rp6000
```

Post-run checks:

```text
blackbird	ok	agent=0.1.0	protocol=1
controller-blackbird-ok
running pid=61559 socket=/run/user/1000/vllm-loader/agent.sock
```

The Blackbird `docker ps` check did not list
`qwen36-27b-fp8-kvfp8-rp6000-vllm-loader` after the smoke, consistent with the
TUI stop path cleaning up the foreground Docker wrapper.

## Coverage Status

Covered in this run:

- P620 controller to Blackbird agent handshake over SSH.
- Agent-owned config/build/model list and config preview.
- Real Blackbird Qwen launch through P620 controller and Blackbird agent.
- Controller-facing READY URL rewritten to `http://10.25.0.51:18003`.
- Normal TUI stop path returned successfully and left the agent reachable.

Not covered in this run:

- Laptop sleep or SSH reconnect gap-free resume.
- Agent daemon restart while a run is live.
- A new build install or model download started during this specific run.

## Follow-up Validation: Registry ID Fix Commit

Commit validated: `f493067`

After the model-entry identity fix, both remote hosts pulled `origin/main`.

P620 controller checks:

```text
blackbird	ok	agent=0.1.0	protocol=1
55 passed, 178 deselected in 2.40s
```

The broader P620 `-k model` slice was intentionally narrowed because four
prepare-launch tests are sensitive to host port `127.0.0.1:8000`, which was
already occupied on P620. The port-safe model-registry/CLI/TUI slice passed.

Blackbird direct validation:

```text
521 passed in 86.57s (0:01:26)
DAEMON_RESTART_LIVE_RUN_OK run_id=daemon-restart-7f6b7294a47c47f8940185a9adf67cc1 port=54781 url=http://127.0.0.1:54781 returncode=0
DISCONNECT_RECONNECT_RESUME_OK run_id=disconnect-reconnect-fd4c91a03a5e4eecafb2dd3e8b1046ac first_seq=1 resume_inode=29097993 resume_offset=34 url=http://127.0.0.1:51951 returncode=0
pinned model	01KT96YKEY13YX7SBHQMHJAS2M	remote-smoke-tiny-current-20260604073848
DONE	c4d5b2cd1afa4706a9574afeb1e507dc	model cached
OK	01KT96YKEY13YX7SBHQMHJAS2M	cached	model metadata is cached
```

This follow-up covered:

- Current commit pull/install on Blackbird.
- Full no-GPU suite and Ruff on Blackbird.
- Agent daemon restart while a fake detached run stayed live.
- Disconnect/reconnect log replay from a durable log cursor.
- Real Hugging Face tiny-model pin, download, and verify through the agent.

Still not covered by this follow-up:

- A new real vLLM build install.
- The full P620-to-Blackbird Qwen launch smoke on commit `f493067`.

## Current-Commit P620 to Blackbird Qwen Smoke

Commit validated: `1c6c0aa`

Pre-run checks:

```text
P620 repo: 1c6c0aa
blackbird	ok	agent=0.1.0	protocol=1
Blackbird repo: 1c6c0aa
GPU unavailable=False note=
GPU 0 NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition mem=2/97887MiB util=0
```

Command:

```bash
ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 \
  -o BatchMode=yes bgconley@10.25.0.50 \
  'set -euo pipefail; cd /home/bgconley/repos/lab-tui;
   timeout 2700 /home/bgconley/venvs/lab-tui/bin/vllm-loader smoke-tui \
     qwen36-27b-fp8-kvfp8-rp6000-blackbird --target blackbird'
```

Output:

```text
READY http://10.25.0.51:18003 models=qwen36-27b-fp8-kvfp8-rp6000
```

Post-run checks:

```text
blackbird	ok	agent=0.1.0	protocol=1
running pid=70725 socket=/run/user/1000/vllm-loader/agent.sock
```

Blackbird `docker ps` produced no active container rows after the smoke,
consistent with the normal TUI stop flow cleaning up the foreground wrapper.
