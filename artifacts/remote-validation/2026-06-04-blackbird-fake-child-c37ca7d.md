# vLLM Loader Remote Validation

- Started: `2026-06-04T17:17:22Z`
- Local commit: `c37ca7d` (`c37ca7d979886c28a94ec9ecef856cb6a26e2b6f`)
- Host: `bgconley@10.25.0.51`
- Remote path: `/home/bgconley/repos/lab-tui`
- Remote venv: `/home/bgconley/venvs/lab-tui`
- Timeout: `1800` seconds
- Real config: `fake-child`
- Build validation: _(not requested)_
- Model validation: _(not requested)_
- SSH command: `ssh -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 -o BatchMode=yes -o StrictHostKeyChecking=accept-new bgconley@10.25.0.51 bash -s -- /home/bgconley/repos/lab-tui 1800 auto /home/bgconley/venvs/lab-tui fake-child `

## Output

```text
== Remote git pull ==
From https://github.com/bgconley/vela
 * branch            main       -> FETCH_HEAD
Already up to date.
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
Requirement already satisfied: httpx>=0.27 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from vllm-loader==0.1.0) (0.28.1)
Requirement already satisfied: huggingface-hub>=0.27 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from vllm-loader==0.1.0) (1.17.0)
Requirement already satisfied: psutil>=5.9 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from vllm-loader==0.1.0) (7.2.2)
Requirement already satisfied: pydantic>=2.8 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from vllm-loader==0.1.0) (2.13.4)
Requirement already satisfied: pyyaml>=6.0 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from vllm-loader==0.1.0) (6.0.3)
Requirement already satisfied: rich>=13.7 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from vllm-loader==0.1.0) (15.0.0)
Requirement already satisfied: textual>=0.86 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from vllm-loader==0.1.0) (8.2.7)
Requirement already satisfied: tqdm>=4.66 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from vllm-loader==0.1.0) (4.67.3)
Requirement already satisfied: typer>=0.12 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from vllm-loader==0.1.0) (0.25.1)
Requirement already satisfied: pytest-asyncio>=0.23 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from vllm-loader==0.1.0) (1.4.0)
Requirement already satisfied: pytest>=8.2 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from vllm-loader==0.1.0) (9.0.3)
Requirement already satisfied: ruff>=0.6 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from vllm-loader==0.1.0) (0.15.15)
Requirement already satisfied: anyio in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from httpx>=0.27->vllm-loader==0.1.0) (4.13.0)
Requirement already satisfied: certifi in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from httpx>=0.27->vllm-loader==0.1.0) (2026.5.20)
Requirement already satisfied: httpcore==1.* in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from httpx>=0.27->vllm-loader==0.1.0) (1.0.9)
Requirement already satisfied: idna in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from httpx>=0.27->vllm-loader==0.1.0) (3.18)
Requirement already satisfied: h11>=0.16 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from httpcore==1.*->httpx>=0.27->vllm-loader==0.1.0) (0.16.0)
Requirement already satisfied: click>=8.4.0 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vllm-loader==0.1.0) (8.4.1)
Requirement already satisfied: filelock>=3.10.0 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vllm-loader==0.1.0) (3.29.1)
Requirement already satisfied: fsspec>=2023.5.0 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vllm-loader==0.1.0) (2026.4.0)
Requirement already satisfied: hf-xet<2.0.0,>=1.4.3 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vllm-loader==0.1.0) (1.5.0)
Requirement already satisfied: packaging>=20.9 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vllm-loader==0.1.0) (26.2)
Requirement already satisfied: typing-extensions>=4.1.0 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from huggingface-hub>=0.27->vllm-loader==0.1.0) (4.15.0)
Requirement already satisfied: shellingham>=1.3.0 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from typer>=0.12->vllm-loader==0.1.0) (1.5.4)
Requirement already satisfied: annotated-doc>=0.0.2 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from typer>=0.12->vllm-loader==0.1.0) (0.0.4)
Requirement already satisfied: annotated-types>=0.6.0 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from pydantic>=2.8->vllm-loader==0.1.0) (0.7.0)
Requirement already satisfied: pydantic-core==2.46.4 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from pydantic>=2.8->vllm-loader==0.1.0) (2.46.4)
Requirement already satisfied: typing-inspection>=0.4.2 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from pydantic>=2.8->vllm-loader==0.1.0) (0.4.2)
Requirement already satisfied: iniconfig>=1.0.1 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from pytest>=8.2->vllm-loader==0.1.0) (2.3.0)
Requirement already satisfied: pluggy<2,>=1.5 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from pytest>=8.2->vllm-loader==0.1.0) (1.6.0)
Requirement already satisfied: pygments>=2.7.2 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from pytest>=8.2->vllm-loader==0.1.0) (2.20.0)
Requirement already satisfied: markdown-it-py>=2.2.0 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from rich>=13.7->vllm-loader==0.1.0) (4.2.0)
Requirement already satisfied: mdurl~=0.1 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from markdown-it-py>=2.2.0->rich>=13.7->vllm-loader==0.1.0) (0.1.2)
Requirement already satisfied: mdit-py-plugins in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from textual>=0.86->vllm-loader==0.1.0) (0.6.1)
Requirement already satisfied: platformdirs<5,>=3.6.0 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from textual>=0.86->vllm-loader==0.1.0) (4.10.0)
Requirement already satisfied: linkify-it-py<3,>=1 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from markdown-it-py[linkify]>=2.1.0->textual>=0.86->vllm-loader==0.1.0) (2.1.0)
Requirement already satisfied: uc-micro-py in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from linkify-it-py<3,>=1->markdown-it-py[linkify]>=2.1.0->textual>=0.86->vllm-loader==0.1.0) (2.0.0)
Building wheels for collected packages: vllm-loader
  Building editable for vllm-loader (pyproject.toml): started
  Building editable for vllm-loader (pyproject.toml): finished with status 'done'
  Created wheel for vllm-loader: filename=vllm_loader-0.1.0-py3-none-any.whl size=2375 sha256=4169b174d712ce6f765d0efa6fee135dce136fc6dd89d77f223be8483795d00e
  Stored in directory: /tmp/pip-ephem-wheel-cache-ugijuzah/wheels/3e/99/96/55613bfa0cc7c533e6eb1028217acc00841afc206a5665afde
Successfully built vllm-loader
Installing collected packages: vllm-loader
  Attempting uninstall: vllm-loader
    Found existing installation: vllm-loader 0.1.0
    Uninstalling vllm-loader-0.1.0:
      Successfully uninstalled vllm-loader-0.1.0
Successfully installed vllm-loader-0.1.0
== Remote agent restart ==
running pid=78383 socket=/run/user/1000/vllm-loader/agent.sock
== Remote host ==
blackbird
GPU unavailable=False note=
GPU 0 NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition GPU-83baa75e-044c-99f9-beb7-bc4139326445 mem=2/97887MiB util=0%
vllm not found on PATH; no-GPU package checks will still run
All checks passed!
........................................................................ [ 12%]
........................................................................ [ 25%]
........................................................................ [ 38%]
........................................................................ [ 51%]
........................................................................ [ 63%]
........................................................................ [ 76%]
........................................................................ [ 89%]
............................................................             [100%]
564 passed in 91.01s (0:01:31)
fake-child	fake/model
qwen3-32b-fp8-62001	/tank/trt/models/Qwen3-32B-FP8
qwen36-27b-fp8-kvfp8-rp6000-blackbird	Qwen/Qwen3.6-27B-FP8
real-vllm-example	mistralai/Mistral-7B-Instruct-v0.3
cwd=/home/bgconley/repos/lab-tui
PYTHONUNBUFFERED=1 ./scripts/fake_vllm_child.py serve fake/model --served-model-name fake-model --host 127.0.0.1 --port 8765
== Daemon restart live-run survival ==
running pid=79020 socket=/run/user/1000/vllm-loader/agent.sock
DAEMON_RESTART_LIVE_RUN_OK run_id=daemon-restart-84d7e65b4668414395ea9cb7282fc100 port=50915 url=http://127.0.0.1:50915 returncode=0
== Disconnect/reconnect stream resume ==
DISCONNECT_RECONNECT_RESUME_OK run_id=disconnect-reconnect-ce2149d79db04bc1af73aac3c58d3c7d first_seq=1 resume_inode=29097991 resume_offset=34 url=http://127.0.0.1:36013 returncode=0
cwd=/home/bgconley/repos/lab-tui
PYTHONUNBUFFERED=1 ./scripts/fake_vllm_child.py serve fake/model --served-model-name fake-model --host 127.0.0.1 --port 8765
READY http://127.0.0.1:8765 models=fake-model
```

## Result

- Completed: `2026-06-04T17:19:08Z`
- Exit status: `0`
