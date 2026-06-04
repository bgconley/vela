# vLLM Loader Remote Validation

- Started: `2026-06-04T19:22:22Z`
- Local commit: `b085610` (`b0856102975324fda7ccf69bb7904a203655f762`)
- Host: `bgconley@10.25.0.50`
- Remote path: `/home/bgconley/repos/lab-tui`
- Remote venv: `/home/bgconley/venvs/lab-tui`
- Pytest args: `-q tests/test_remote_workflow.py tests/test_transport_factory.py tests/test_targets.py`
- Remote target: `blackbird`
- Timeout: `2700` seconds
- Real config: `qwen36-27b-fp8-kvfp8-rp6000-blackbird`
- Real resume validation: _(not requested)_
- Build validation: _(not requested)_
- Model validation: _(not requested)_
- SSH command: `ssh -A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 -o BatchMode=yes bgconley@10.25.0.50 env VLLM_LOADER_REMOTE_TARGET=blackbird VLLM_LOADER_REMOTE_PYTEST_ARGS=-q\\\ tests/test_remote_workflow.py\\\ tests/test_transport_factory.py\\\ tests/test_targets.py bash -s -- /home/bgconley/repos/lab-tui 2700 auto /home/bgconley/venvs/lab-tui qwen36-27b-fp8-kvfp8-rp6000-blackbird `

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
Requirement already satisfied: annotated-types>=0.6.0 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from pydantic>=2.8->vllm-loader==0.1.0) (0.7.0)
Requirement already satisfied: pydantic-core==2.46.4 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from pydantic>=2.8->vllm-loader==0.1.0) (2.46.4)
Requirement already satisfied: typing-inspection>=0.4.2 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from pydantic>=2.8->vllm-loader==0.1.0) (0.4.2)
Requirement already satisfied: iniconfig>=1.0.1 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from pytest>=8.2->vllm-loader==0.1.0) (2.3.0)
Requirement already satisfied: pluggy<2,>=1.5 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from pytest>=8.2->vllm-loader==0.1.0) (1.6.0)
Requirement already satisfied: pygments>=2.7.2 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from pytest>=8.2->vllm-loader==0.1.0) (2.20.0)
Requirement already satisfied: markdown-it-py>=2.2.0 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from rich>=13.7->vllm-loader==0.1.0) (4.2.0)
Requirement already satisfied: mdit-py-plugins in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from textual>=0.86->vllm-loader==0.1.0) (0.6.1)
Requirement already satisfied: platformdirs<5,>=3.6.0 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from textual>=0.86->vllm-loader==0.1.0) (4.10.0)
Requirement already satisfied: shellingham>=1.3.0 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from typer>=0.12->vllm-loader==0.1.0) (1.5.4)
Requirement already satisfied: annotated-doc>=0.0.2 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from typer>=0.12->vllm-loader==0.1.0) (0.0.4)
Requirement already satisfied: mdurl~=0.1 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from markdown-it-py>=2.2.0->rich>=13.7->vllm-loader==0.1.0) (0.1.2)
Requirement already satisfied: linkify-it-py<3,>=1 in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from markdown-it-py[linkify]>=2.1.0->textual>=0.86->vllm-loader==0.1.0) (2.1.0)
Requirement already satisfied: uc-micro-py in /home/bgconley/venvs/lab-tui/lib/python3.12/site-packages (from linkify-it-py<3,>=1->markdown-it-py[linkify]>=2.1.0->textual>=0.86->vllm-loader==0.1.0) (2.0.0)
Checking if build backend supports build_editable: started
Checking if build backend supports build_editable: finished with status 'done'
Building wheels for collected packages: vllm-loader
  Building editable for vllm-loader (pyproject.toml): started
  Building editable for vllm-loader (pyproject.toml): finished with status 'done'
  Created wheel for vllm-loader: filename=vllm_loader-0.1.0-py3-none-any.whl size=4192 sha256=4878a37acfd3b19a36acac30e4d60597224813abc5e66106c3aec6b192e73d2b
  Stored in directory: /tmp/pip-ephem-wheel-cache-m1uii5dl/wheels/3e/99/96/55613bfa0cc7c533e6eb1028217acc00841afc206a5665afde
Successfully built vllm-loader
Installing collected packages: vllm-loader
  Attempting uninstall: vllm-loader
    Found existing installation: vllm-loader 0.1.0
    Uninstalling vllm-loader-0.1.0:
      Successfully uninstalled vllm-loader-0.1.0
Successfully installed vllm-loader-0.1.0
== Remote agent restart ==
running pid=1958176 socket=/run/user/1000/vllm-loader/agent.sock
== Remote host ==
620-01
GPU unavailable=False note=
GPU 0 NVIDIA RTX PRO 4000 Blackwell GPU-103aae53-b1be-c275-656e-82515763d051 mem=22003/24467MiB util=0%
GPU 1 NVIDIA RTX PRO 4000 Blackwell GPU-6ec4ee66-142e-34ad-e17d-a131d7153b51 mem=23567/24467MiB util=0%
vllm not found on PATH; no-GPU package checks will still run
All checks passed!
....................................                                     [100%]
36 passed in 0.30s
fake-child	fake/model
qwen3-32b-fp8-62001	/tank/trt/models/Qwen3-32B-FP8
qwen36-27b-fp8-kvfp8-rp6000-blackbird	Qwen/Qwen3.6-27B-FP8
real-vllm-example	mistralai/Mistral-7B-Instruct-v0.3
tiny-random-llama-detached-blackbird	hf-internal-testing/tiny-random-LlamaForCausalLM
cwd=/home/bgconley/repos/lab-tui
PYTHONUNBUFFERED=1 ./scripts/fake_vllm_child.py serve fake/model --served-model-name fake-model --host 127.0.0.1 --port 8765
== Daemon restart live-run survival ==
running pid=1958331 socket=/run/user/1000/vllm-loader/agent.sock
DAEMON_RESTART_LIVE_RUN_OK run_id=daemon-restart-f836f4b469984261bf2b5655aef3993c port=60175 url=http://127.0.0.1:60175 returncode=0
== Disconnect/reconnect stream resume ==
DISCONNECT_RECONNECT_RESUME_OK run_id=disconnect-reconnect-369e1100cd7d43a4a46e6eb19d949d1a first_seq=1 resume_inode=39485326 resume_offset=34 url=http://127.0.0.1:59241 returncode=0
cwd=/home/bgconley/repos/lab-tui
CONTAINER=qwen36-27b-fp8-kvfp8-rp6000-vllm-loader HF_CACHE_ROOT=/home/bgconley/models/qwen36-dual-fp8-vlm/hf-cache PULL_IMAGE=0 PYTHONUNBUFFERED=1 ROOT=/home/bgconley/models/qwen36-27b-fp8-rp6000 VLLM_API_KEY='••••' ./scripts/blackbird_qwen36_vllm_foreground.sh serve Qwen/Qwen3.6-27B-FP8 --served-model-name qwen36-27b-fp8-kvfp8-rp6000 --host 0.0.0.0 --port 18003 --gpu-memory-utilization 0.97 --max-model-len 262144 --dtype auto --kv-cache-dtype fp8 --max-num-seqs 16 --disable-access-log-for-endpoints /health --kv-cache-memory-bytes 64424509440 --max-num-batched-tokens 8192 --max-num-partial-prefills 1 --max-long-partial-prefills 1 --attention-backend FLASHINFER --trust-remote-code --language-model-only --enable-chunked-prefill --enable-prefix-caching --enable-auto-tool-choice --reasoning-parser qwen3 --tool-call-parser qwen3_coder --limit-mm-per-prompt '{"image":0,"video":0}' --compilation-config '{"cudagraph_capture_sizes":[1,2,4,8,16],"cudagraph_num_of_warmups":1}' --cudagraph-metrics --disable-uvicorn-access-log
WARNING: Binds vLLM to 0.0.0.0, reachable beyond localhost. `--api-key` does not protect all endpoints, including `/invocations`; put it behind a reverse proxy or firewall.
READY http://10.25.0.51:18003 models=qwen36-27b-fp8-kvfp8-rp6000
```

## Result

- Completed: `2026-06-04T19:23:34Z`
- Exit status: `0`
