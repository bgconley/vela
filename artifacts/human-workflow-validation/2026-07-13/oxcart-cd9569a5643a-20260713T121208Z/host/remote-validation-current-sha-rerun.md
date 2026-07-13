# Vela Remote Validation

- Started: `2026-07-13T13:35:20Z`
- Local commit: `cd9569a` (`cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7`)
- Requested branch: `remediate/2026-07-09-review`
- Expected remote commit: `cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7`
- Host: `bgconley@10.25.0.50`
- Remote path: `/home/bgconley/repos/lab-tui`
- Remote venv: `/tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv`
- Pytest args: `-q tests/test_remote_workflow.py tests/test_transport_factory.py tests/test_targets.py`
- Remote target: _(default)_
- Timeout: `1800` seconds
- Real config: _(none)_
- Real resume validation: _(not requested)_
- Build validation: _(not requested)_
- Model validation: _(not requested)_
- Gated model auth validation: _(not requested)_
- SSH command: `ssh -o BatchMode=yes -o ConnectTimeout=15 bgconley@10.25.0.50 env VELA_REMOTE_BRANCH=remediate/2026-07-09-review VELA_REMOTE_EXPECTED_SHA=cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7 VELA_REMOTE_AGENT_RUNTIME_DIR=/tmp/vela-agent-cd9569a VELA_REMOTE_PYTEST_ARGS=-q\\\ tests/test_remote_workflow.py\\\ tests/test_transport_factory.py\\\ tests/test_targets.py bash -s -- /home/bgconley/repos/lab-tui 1800 auto /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv `

## Output

```text
== Remote git revision (remediate/2026-07-09-review) ==
HEAD is now at cd9569a fix: render deployment data as literal text
REMOTE_REVISION_OK expected=cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7 actual=cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7 branch=remediate/2026-07-09-review source=owned-worktree
Preparing worktree (detached HEAD cd9569a)
Processing /tmp/vela-remote-validation.Lk9dvE/checkout
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
  Getting requirements to build wheel: started
  Getting requirements to build wheel: finished with status 'done'
  Preparing metadata (pyproject.toml): started
  Preparing metadata (pyproject.toml): finished with status 'done'
Requirement already satisfied: httpx>=0.27 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (0.28.1)
Requirement already satisfied: huggingface-hub>=0.27 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (1.23.0)
Requirement already satisfied: psutil>=5.9 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (7.2.2)
Requirement already satisfied: pydantic>=2.8 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (2.13.4)
Requirement already satisfied: pyyaml>=6.0 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (6.0.3)
Requirement already satisfied: rich>=13.7 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (15.0.0)
Requirement already satisfied: textual<9,>=8.2 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (8.2.8)
Requirement already satisfied: tqdm>=4.66 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (4.68.4)
Requirement already satisfied: typer>=0.12 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (0.26.8)
Requirement already satisfied: mypy>=1.8 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (2.3.0)
Requirement already satisfied: pytest-asyncio>=0.24 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (1.4.0)
Requirement already satisfied: pytest-timeout>=2.3 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (2.4.0)
Requirement already satisfied: pytest>=8.2 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (9.1.1)
Requirement already satisfied: ruff>=0.6 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from vela==0.1.0) (0.15.21)
Requirement already satisfied: anyio in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from httpx>=0.27->vela==0.1.0) (4.14.2)
Requirement already satisfied: certifi in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from httpx>=0.27->vela==0.1.0) (2026.6.17)
Requirement already satisfied: httpcore==1.* in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from httpx>=0.27->vela==0.1.0) (1.0.9)
Requirement already satisfied: idna in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from httpx>=0.27->vela==0.1.0) (3.18)
Requirement already satisfied: h11>=0.16 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from httpcore==1.*->httpx>=0.27->vela==0.1.0) (0.16.0)
Requirement already satisfied: click<9.0.0,>=8.4.2 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (8.4.2)
Requirement already satisfied: filelock>=3.10.0 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (3.29.7)
Requirement already satisfied: fsspec>=2023.5.0 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (2026.6.0)
Requirement already satisfied: hf-xet<2.0.0,>=1.5.1 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (1.5.1)
Requirement already satisfied: packaging>=20.9 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (26.2)
Requirement already satisfied: typing-extensions>=4.1.0 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (4.16.0)
Requirement already satisfied: mypy_extensions>=1.0.0 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from mypy>=1.8->vela==0.1.0) (1.1.0)
Requirement already satisfied: pathspec>=1.0.0 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from mypy>=1.8->vela==0.1.0) (1.1.1)
Requirement already satisfied: librt>=0.13.0 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from mypy>=1.8->vela==0.1.0) (0.13.0)
Requirement already satisfied: ast-serialize<1.0.0,>=0.6.0 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from mypy>=1.8->vela==0.1.0) (0.6.0)
Requirement already satisfied: annotated-types>=0.6.0 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from pydantic>=2.8->vela==0.1.0) (0.7.0)
Requirement already satisfied: pydantic-core==2.46.4 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from pydantic>=2.8->vela==0.1.0) (2.46.4)
Requirement already satisfied: typing-inspection>=0.4.2 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from pydantic>=2.8->vela==0.1.0) (0.4.2)
Requirement already satisfied: iniconfig>=1.0.1 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from pytest>=8.2->vela==0.1.0) (2.3.0)
Requirement already satisfied: pluggy<2,>=1.5 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from pytest>=8.2->vela==0.1.0) (1.6.0)
Requirement already satisfied: pygments>=2.7.2 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from pytest>=8.2->vela==0.1.0) (2.20.0)
Requirement already satisfied: markdown-it-py>=2.2.0 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from rich>=13.7->vela==0.1.0) (4.2.0)
Requirement already satisfied: mdit-py-plugins in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from textual<9,>=8.2->vela==0.1.0) (0.6.1)
Requirement already satisfied: platformdirs<5,>=3.6.0 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from textual<9,>=8.2->vela==0.1.0) (4.10.0)
Requirement already satisfied: shellingham>=1.3.0 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from typer>=0.12->vela==0.1.0) (1.5.4)
Requirement already satisfied: annotated-doc>=0.0.2 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from typer>=0.12->vela==0.1.0) (0.0.4)
Requirement already satisfied: mdurl~=0.1 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from markdown-it-py>=2.2.0->rich>=13.7->vela==0.1.0) (0.1.2)
Requirement already satisfied: linkify-it-py<3,>=1 in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from markdown-it-py[linkify]>=2.1.0->textual<9,>=8.2->vela==0.1.0) (2.1.0)
Requirement already satisfied: uc-micro-py in /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv/lib/python3.12/site-packages (from linkify-it-py<3,>=1->markdown-it-py[linkify]>=2.1.0->textual<9,>=8.2->vela==0.1.0) (2.0.0)
Building wheels for collected packages: vela
  Building wheel for vela (pyproject.toml): started
  Building wheel for vela (pyproject.toml): finished with status 'done'
  Created wheel for vela: filename=vela-0.1.0-py3-none-any.whl size=340958 sha256=41763f0a6c1cf84b2d74135ddf819b527b7e1bd92d8fa8fe3a708404328525e6
  Stored in directory: /tmp/pip-ephem-wheel-cache-e113gg5m/wheels/fc/e2/b4/18bc5722bf64d5e440ee066e903bdfee726b3f4be85c9707bd
Successfully built vela
Installing collected packages: vela
  Attempting uninstall: vela
    Found existing installation: vela 0.1.0
    Uninstalling vela-0.1.0:
      Successfully uninstalled vela-0.1.0
Successfully installed vela-0.1.0
== Remote agent restart ==
running pid=1385408 socket=/tmp/vela-agent-cd9569a/agent.sock
== Remote host ==
oxcart
GPU unavailable=False note=
GPU 0 NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition GPU-aebb15f2-0592-40da-7d2e-77d70241825c mem=2/97887MiB util=0%
vllm not found on PATH; no-GPU package checks will still run
All checks passed!
........................................................................ [ 43%]
........................................................................ [ 87%]
.....................                                                    [100%]
165 passed in 1.27s
fake-child	fake/model
oxcart-qwen36-27b-fp8-mtp-vl	Qwen/Qwen3.6-27B-FP8
qwen3-32b-fp8-62001	/tank/trt/models/Qwen3-32B-FP8
qwen36-27b-bf16-rp6000-blackbird	Qwen/Qwen3.6-27B
qwen36-27b-fp8-kvfp8-rp6000-blackbird	Qwen/Qwen3.6-27B-FP8
real-vllm-example	mistralai/Mistral-7B-Instruct-v0.3
tiny-random-llama-detached-blackbird	hf-internal-testing/tiny-random-LlamaForCausalLM
cwd=/tmp/vela-remote-validation.Lk9dvE/checkout
PYTHONUNBUFFERED=1
./scripts/fake_vllm_child.py serve fake/model --served-model-name fake-model --host 127.0.0.1 --port 8765
== Daemon restart live-run survival ==
running pid=1385808 socket=/tmp/vela-agent-cd9569a/agent.sock
DAEMON_RESTART_LIVE_RUN_OK run_id=daemon-restart-20b9b5be7406496bbd26f5f4afbd4069 port=60867 url=http://127.0.0.1:60867 returncode=0
== Disconnect/reconnect stream resume ==
DISCONNECT_RECONNECT_RESUME_OK run_id=disconnect-reconnect-3c6c3075fd9d4956821bcc5e8bbe5def first_seq=1 resume_inode=1848354 resume_offset=34 url=http://127.0.0.1:50979 returncode=0
```

## Result

- Completed: `2026-07-13T13:35:36Z`
- Exit status: `0`
