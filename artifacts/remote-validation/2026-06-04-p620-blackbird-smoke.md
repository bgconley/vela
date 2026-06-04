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
