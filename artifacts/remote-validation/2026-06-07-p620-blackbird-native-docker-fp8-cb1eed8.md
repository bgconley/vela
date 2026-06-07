# Vela Remote Validation

- Started: `2026-06-07T05:32:07Z`
- Local commit: `cb1eed8` (`cb1eed8c30ecf0a854a2a8db621a5d6c29282522`)
- Host: `bgconley@10.25.0.50`
- Remote path: `/home/bgconley/repos/lab-tui`
- Remote venv: `/tank/venvs/lab-tui`
- Pytest args: `-q`
- Remote target: `blackbird`
- Timeout: `2700` seconds
- Real config: `qwen36-27b-fp8-kvfp8-rp6000-blackbird`
- Real resume validation: _(not requested)_
- Build validation: _(not requested)_
- Model validation: _(not requested)_
- Gated model auth validation: _(not requested)_
- SSH command: `ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 -o BatchMode=yes bgconley@10.25.0.50 env VELA_REMOTE_TARGET=blackbird bash -s -- /home/bgconley/repos/lab-tui 2700 auto /tank/venvs/lab-tui qwen36-27b-fp8-kvfp8-rp6000-blackbird `

## Output

```text
== Remote git pull ==
Updating e3a5c6d..cb1eed8
Fast-forward
From https://github.com/bgconley/vela
 * branch            main       -> FETCH_HEAD
   e3a5c6d..cb1eed8  main       -> origin/main
 .wolf/buglog.json                                  |   17 +
 .wolf/memory.md                                    |    1 +
 README.md                                          |   12 +-
 ...-07-p620-blackbird-native-docker-fp8-e3a5c6d.md | 1615 ++++++++++++++++++++
 docs/configuration.md                              |    6 +-
 docs/gpu-workflow.md                               |   20 +-
 tests/test_agent_client.py                         |   75 +-
 tests/test_branding.py                             |    8 +
 tests/test_cli_run.py                              |    4 +-
 tests/test_docs.py                                 |    3 +-
 tests/test_tui_smoke.py                            |    8 +-
 11 files changed, 1727 insertions(+), 42 deletions(-)
 create mode 100644 artifacts/remote-validation/2026-06-07-p620-blackbird-native-docker-fp8-e3a5c6d.md
Obtaining file:///home/bgconley/repos/lab-tui
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
  Checking if build backend supports build_editable: started
  Checking if build backend supports build_editable: finished with status 'done'
  Getting requirements to build editable: started
  Getting requirements to build editable: finished with status 'done'
  Installing backend dependencies: started
  Installing backend dependencies: finished with status 'done'
  Preparing editable metadata (pyproject.toml): started
  Preparing editable metadata (pyproject.toml): finished with status 'done'
Requirement already satisfied: httpx>=0.27 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (0.28.1)
Requirement already satisfied: huggingface-hub>=0.27 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (1.18.0)
Requirement already satisfied: psutil>=5.9 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (7.2.2)
Requirement already satisfied: pydantic>=2.8 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (2.13.4)
Requirement already satisfied: pyyaml>=6.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (6.0.3)
Requirement already satisfied: rich>=13.7 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (15.0.0)
Requirement already satisfied: textual>=0.86 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (8.2.7)
Requirement already satisfied: tqdm>=4.66 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (4.68.1)
Requirement already satisfied: typer>=0.12 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (0.25.1)
Requirement already satisfied: pytest-asyncio>=0.24 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (1.4.0)
Requirement already satisfied: pytest>=8.2 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (9.0.3)
Requirement already satisfied: ruff>=0.6 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from vela==0.1.0) (0.15.15)
Requirement already satisfied: anyio in /tank/venvs/lab-tui/lib/python3.12/site-packages (from httpx>=0.27->vela==0.1.0) (4.13.0)
Requirement already satisfied: certifi in /tank/venvs/lab-tui/lib/python3.12/site-packages (from httpx>=0.27->vela==0.1.0) (2026.5.20)
Requirement already satisfied: httpcore==1.* in /tank/venvs/lab-tui/lib/python3.12/site-packages (from httpx>=0.27->vela==0.1.0) (1.0.9)
Requirement already satisfied: idna in /tank/venvs/lab-tui/lib/python3.12/site-packages (from httpx>=0.27->vela==0.1.0) (3.18)
Requirement already satisfied: h11>=0.16 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from httpcore==1.*->httpx>=0.27->vela==0.1.0) (0.16.0)
Requirement already satisfied: click>=8.4.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (8.4.1)
Requirement already satisfied: filelock>=3.10.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (3.29.1)
Requirement already satisfied: fsspec>=2023.5.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (2026.4.0)
Requirement already satisfied: hf-xet<2.0.0,>=1.4.3 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (1.5.0)
Requirement already satisfied: packaging>=20.9 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (26.2)
Requirement already satisfied: typing-extensions>=4.1.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vela==0.1.0) (4.15.0)
Requirement already satisfied: annotated-types>=0.6.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from pydantic>=2.8->vela==0.1.0) (0.7.0)
Requirement already satisfied: pydantic-core==2.46.4 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from pydantic>=2.8->vela==0.1.0) (2.46.4)
Requirement already satisfied: typing-inspection>=0.4.2 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from pydantic>=2.8->vela==0.1.0) (0.4.2)
Requirement already satisfied: iniconfig>=1.0.1 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from pytest>=8.2->vela==0.1.0) (2.3.0)
Requirement already satisfied: pluggy<2,>=1.5 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from pytest>=8.2->vela==0.1.0) (1.6.0)
Requirement already satisfied: pygments>=2.7.2 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from pytest>=8.2->vela==0.1.0) (2.20.0)
Requirement already satisfied: markdown-it-py>=2.2.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from rich>=13.7->vela==0.1.0) (4.2.0)
Requirement already satisfied: mdit-py-plugins in /tank/venvs/lab-tui/lib/python3.12/site-packages (from textual>=0.86->vela==0.1.0) (0.6.1)
Requirement already satisfied: platformdirs<5,>=3.6.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from textual>=0.86->vela==0.1.0) (4.10.0)
Requirement already satisfied: shellingham>=1.3.0 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from typer>=0.12->vela==0.1.0) (1.5.4)
Requirement already satisfied: annotated-doc>=0.0.2 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from typer>=0.12->vela==0.1.0) (0.0.4)
Requirement already satisfied: mdurl~=0.1 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from markdown-it-py>=2.2.0->rich>=13.7->vela==0.1.0) (0.1.2)
Requirement already satisfied: linkify-it-py<3,>=1 in /tank/venvs/lab-tui/lib/python3.12/site-packages (from markdown-it-py[linkify]>=2.1.0->textual>=0.86->vela==0.1.0) (2.1.0)
Requirement already satisfied: uc-micro-py in /tank/venvs/lab-tui/lib/python3.12/site-packages (from linkify-it-py<3,>=1->markdown-it-py[linkify]>=2.1.0->textual>=0.86->vela==0.1.0) (2.0.0)
Checking if build backend supports build_editable: started
Checking if build backend supports build_editable: finished with status 'done'
Building wheels for collected packages: vela
  Building editable for vela (pyproject.toml): started
  Building editable for vela (pyproject.toml): finished with status 'done'
  Created wheel for vela: filename=vela-0.1.0-py3-none-any.whl size=4901 sha256=7b97352cf05a62c7c4af2656a9068be6299c987f28a564817f9d137faf22655c
  Stored in directory: /tmp/pip-ephem-wheel-cache-uyiatoot/wheels/3e/99/96/55613bfa0cc7c533e6eb1028217acc00841afc206a5665afde
Successfully built vela
Installing collected packages: vela
  Attempting uninstall: vela
    Found existing installation: vela 0.1.0
    Uninstalling vela-0.1.0:
      Successfully uninstalled vela-0.1.0
Successfully installed vela-0.1.0
== Remote agent restart ==
running pid=3006639 socket=/run/user/1000/vela/agent.sock
== Remote host ==
620-01
GPU unavailable=False note=
GPU 0 NVIDIA RTX PRO 4000 Blackwell GPU-103aae53-b1be-c275-656e-82515763d051 mem=22003/24467MiB util=0%
GPU 1 NVIDIA RTX PRO 4000 Blackwell GPU-6ec4ee66-142e-34ad-e17d-a131d7153b51 mem=23559/24467MiB util=0%
vllm not found on PATH; no-GPU package checks will still run
All checks passed!
........................................................................ [  7%]
........................................................................ [ 15%]
........................................................................ [ 23%]
........................................................................ [ 30%]
........................................................................ [ 38%]
........................................................................ [ 46%]
........................................................................ [ 53%]
........................................................................ [ 61%]
........................................................................ [ 69%]
........................................................................ [ 77%]
........................................................................ [ 84%]
........................................................................ [ 92%]
......................................................................   [100%]
934 passed in 138.72s (0:02:18)
fake-child	fake/model
qwen3-32b-fp8-62001	/tank/trt/models/Qwen3-32B-FP8
qwen36-27b-bf16-rp6000-blackbird	Qwen/Qwen3.6-27B
qwen36-27b-fp8-kvfp8-rp6000-blackbird	Qwen/Qwen3.6-27B-FP8
real-vllm-example	mistralai/Mistral-7B-Instruct-v0.3
tiny-random-llama-detached-blackbird	hf-internal-testing/tiny-random-LlamaForCausalLM
cwd=/home/bgconley/repos/lab-tui
PYTHONUNBUFFERED=1 ./scripts/fake_vllm_child.py serve fake/model --served-model-name fake-model --host 127.0.0.1 --port 8765
== Daemon restart live-run survival ==
running pid=3011327 socket=/run/user/1000/vela/agent.sock
DAEMON_RESTART_LIVE_RUN_OK run_id=daemon-restart-38af4694fe6e41319f78792ff28876f7 port=60991 url=http://127.0.0.1:60991 returncode=0
== Disconnect/reconnect stream resume ==
DISCONNECT_RECONNECT_RESUME_OK run_id=disconnect-reconnect-95f0d5e973cd44b297d988cf2693d008 first_seq=1 resume_inode=17956921 resume_offset=34 url=http://127.0.0.1:55981 returncode=0
ERROR: target agent does not support requested capabilities
```

## Result

- Completed: `2026-06-07T05:34:44Z`
- Exit status: `2`
