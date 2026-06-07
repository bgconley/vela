# Vela Remote Validation

- Started: `2026-06-07T05:36:07Z`
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
  Stored in directory: /tmp/pip-ephem-wheel-cache-uwknmuz6/wheels/3e/99/96/55613bfa0cc7c533e6eb1028217acc00841afc206a5665afde
Successfully built vela
Installing collected packages: vela
  Attempting uninstall: vela
    Found existing installation: vela 0.1.0
    Uninstalling vela-0.1.0:
      Successfully uninstalled vela-0.1.0
Successfully installed vela-0.1.0
== Remote agent restart ==
running pid=3014587 socket=/run/user/1000/vela/agent.sock
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
934 passed in 137.57s (0:02:17)
fake-child	fake/model
qwen3-32b-fp8-62001	/tank/trt/models/Qwen3-32B-FP8
qwen36-27b-bf16-rp6000-blackbird	Qwen/Qwen3.6-27B
qwen36-27b-fp8-kvfp8-rp6000-blackbird	Qwen/Qwen3.6-27B-FP8
real-vllm-example	mistralai/Mistral-7B-Instruct-v0.3
tiny-random-llama-detached-blackbird	hf-internal-testing/tiny-random-LlamaForCausalLM
cwd=/home/bgconley/repos/lab-tui
PYTHONUNBUFFERED=1 ./scripts/fake_vllm_child.py serve fake/model --served-model-name fake-model --host 127.0.0.1 --port 8765
== Daemon restart live-run survival ==
running pid=3019245 socket=/run/user/1000/vela/agent.sock
DAEMON_RESTART_LIVE_RUN_OK run_id=daemon-restart-22d921da2c6a48d9bee6e47e79498eb1 port=43147 url=http://127.0.0.1:43147 returncode=0
== Disconnect/reconnect stream resume ==
DISCONNECT_RECONNECT_RESUME_OK run_id=disconnect-reconnect-21e0088906b74513ae0e2734970fb91f first_seq=1 resume_inode=17956921 resume_offset=34 url=http://127.0.0.1:45225 returncode=0
cwd=/home/bgconley/repos/lab-tui
FLASHINFER_CUDA_ARCH_LIST=12.0f FLASHINFER_JIT_VERBOSE=0 FLASHINFER_LOGLEVEL=0 HF_HOME=/root/.cache/huggingface HF_HUB_CACHE=/root/.cache/huggingface/hub PYTHONUNBUFFERED=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True SAFETENSORS_FAST_GPU=1 TORCHINDUCTOR_CACHE_DIR=/root/.cache/torch TRITON_CACHE_DIR=/root/.cache/triton VLLM_API_KEY='••••' VLLM_CACHE_ROOT=/root/.cache/vllm docker run -d --name qwen36-27b-fp8-kvfp8-rp6000-vela --gpus all --network host --ipc=host --shm-size 32g --restart no -e FLASHINFER_CUDA_ARCH_LIST -e FLASHINFER_JIT_VERBOSE -e FLASHINFER_LOGLEVEL -e HF_HOME -e HF_HUB_CACHE -e PYTHONUNBUFFERED -e PYTORCH_CUDA_ALLOC_CONF -e SAFETENSORS_FAST_GPU -e TORCHINDUCTOR_CACHE_DIR -e TRITON_CACHE_DIR -e VLLM_API_KEY -e VLLM_CACHE_ROOT -v /home/bgconley/models/qwen36-27b-fp8-rp6000/vllm-cache:/root/.cache/vllm -v /home/bgconley/models/qwen36-27b-fp8-rp6000/triton-cache:/root/.cache/triton -v /home/bgconley/models/qwen36-27b-fp8-rp6000/torch-compile-cache:/root/.cache/torch -v /home/bgconley/models/qwen36-27b-fp8-rp6000/flashinfer-cache:/root/.cache/flashinfer -v /home/bgconley/models/qwen36-27b-fp8-rp6000/tmp:/tmp/qwen36-27b-fp8-rp6000 -v /home/bgconley/models/qwen36-dual-fp8-vlm/hf-cache:/root/.cache/huggingface:rw --ulimit memlock=-1 --ulimit stack=67108864 vllm/vllm-openai@sha256:b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046 Qwen/Qwen3.6-27B-FP8 --served-model-name qwen36-27b-fp8-kvfp8-rp6000 --host 0.0.0.0 --port 18003 --gpu-memory-utilization 0.97 --max-model-len 262144 --dtype auto --kv-cache-dtype fp8 --max-num-seqs 16 --disable-access-log-for-endpoints /health --kv-cache-memory-bytes 64424509440 --max-num-batched-tokens 8192 --max-num-partial-prefills 1 --max-long-partial-prefills 1 --attention-backend FLASHINFER --trust-remote-code --language-model-only --enable-chunked-prefill --enable-prefix-caching --enable-auto-tool-choice --reasoning-parser qwen3 --tool-call-parser qwen3_coder --limit-mm-per-prompt '{"image":0,"video":0}' --compilation-config '{"cudagraph_capture_sizes":[1,2,4,8,16],"cudagraph_num_of_warmups":1}' --cudagraph-metrics --disable-uvicorn-access-log
WARNING: Binds vLLM to 0.0.0.0, reachable beyond localhost. `--api-key` does not protect all endpoints, including `/invocations`; put it behind a reverse proxy or firewall.
READY http://10.25.0.51:18003 models=qwen36-27b-fp8-kvfp8-rp6000 run_id=42ac039308284cf7ba4deabbbffc5ea4
VELA_SMOKE_RUN_ID	42ac039308284cf7ba4deabbbffc5ea4
BACKEND_EVIDENCE_ERROR config=qwen36-27b-fp8-kvfp8-rp6000-blackbird run_id=42ac039308284cf7ba4deabbbffc5ea4 detail=unknown detached run: 42ac039308284cf7ba4deabbbffc5ea4
```

## Result

- Completed: `2026-06-07T05:39:47Z`
- Exit status: `2`
