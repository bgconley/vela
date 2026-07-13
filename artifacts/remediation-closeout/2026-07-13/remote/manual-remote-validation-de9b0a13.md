# Vela Remote Validation

- Started: `2026-07-13T07:54:16Z`
- Local commit: `de9b0a1` (`de9b0a13f2ef7166014794bb211845dd3ba96123`)
- Requested branch: `remediate/2026-07-09-review`
- Expected remote commit: `de9b0a13f2ef7166014794bb211845dd3ba96123`
- Host: `bgconley@10.25.0.50`
- Remote path: `/home/bgconley/repos/lab-tui`
- Remote venv: `/tank/venvs/lab-tui-remediation-de9b0a1`
- Pytest args: `-q tests/test_remote_workflow.py tests/test_transport_factory.py tests/test_targets.py`
- Remote target: _(default)_
- Timeout: `1800` seconds
- Real config: _(none)_
- Real resume validation: _(not requested)_
- Build validation: _(not requested)_
- Model validation: _(not requested)_
- Gated model auth validation: _(not requested)_
- SSH command: `ssh -o BatchMode=yes -o ConnectTimeout=15 bgconley@10.25.0.50 env VELA_REMOTE_BRANCH=remediate/2026-07-09-review VELA_REMOTE_EXPECTED_SHA=de9b0a13f2ef7166014794bb211845dd3ba96123 VELA_REMOTE_AGENT_RUNTIME_DIR=/tank/venvs/lab-tui-remediation-de9b0a1/agent-runtime VELA_REMOTE_PYTEST_ARGS=-q\\\ tests/test_remote_workflow.py\\\ tests/test_transport_factory.py\\\ tests/test_targets.py bash -s -- /home/bgconley/repos/lab-tui 1800 auto /tank/venvs/lab-tui-remediation-de9b0a1 `

## Output

```text
== Remote git revision (remediate/2026-07-09-review) ==
From https://github.com/bgconley/vela
 * [new branch]      remediate/2026-07-09-review -> origin/remediate/2026-07-09-review
Preparing worktree (detached HEAD de9b0a1)
HEAD is now at de9b0a1 fix: close verified vela remediation gaps
REMOTE_REVISION_OK expected=de9b0a13f2ef7166014794bb211845dd3ba96123 actual=de9b0a13f2ef7166014794bb211845dd3ba96123 branch=remediate/2026-07-09-review source=owned-worktree
Processing /tmp/vela-remote-validation.91cKX1/checkout
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
  Getting requirements to build wheel: started
  Getting requirements to build wheel: finished with status 'done'
  Preparing metadata (pyproject.toml): started
  Preparing metadata (pyproject.toml): finished with status 'done'
Collecting httpx>=0.27 (from vela==0.1.0)
  Using cached httpx-0.28.1-py3-none-any.whl.metadata (7.1 kB)
Collecting huggingface-hub>=0.27 (from vela==0.1.0)
  Downloading huggingface_hub-1.23.0-py3-none-any.whl.metadata (14 kB)
Collecting psutil>=5.9 (from vela==0.1.0)
  Using cached psutil-7.2.2-cp36-abi3-manylinux2010_x86_64.manylinux_2_12_x86_64.manylinux_2_28_x86_64.whl.metadata (22 kB)
Collecting pydantic>=2.8 (from vela==0.1.0)
  Using cached pydantic-2.13.4-py3-none-any.whl.metadata (109 kB)
Collecting pyyaml>=6.0 (from vela==0.1.0)
  Using cached pyyaml-6.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (2.4 kB)
Collecting rich>=13.7 (from vela==0.1.0)
  Using cached rich-15.0.0-py3-none-any.whl.metadata (18 kB)
Collecting textual<9,>=8.2 (from vela==0.1.0)
  Downloading textual-8.2.8-py3-none-any.whl.metadata (9.1 kB)
Collecting tqdm>=4.66 (from vela==0.1.0)
  Downloading tqdm-4.68.4-py3-none-any.whl.metadata (57 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 57.4/57.4 kB 1.8 MB/s eta 0:00:00
Collecting typer>=0.12 (from vela==0.1.0)
  Downloading typer-0.26.8-py3-none-any.whl.metadata (15 kB)
Collecting mypy>=1.8 (from vela==0.1.0)
  Downloading mypy-2.2.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (2.4 kB)
Collecting pytest-asyncio>=0.24 (from vela==0.1.0)
  Using cached pytest_asyncio-1.4.0-py3-none-any.whl.metadata (4.1 kB)
Collecting pytest-timeout>=2.3 (from vela==0.1.0)
  Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)
Collecting pytest>=8.2 (from vela==0.1.0)
  Downloading pytest-9.1.1-py3-none-any.whl.metadata (7.6 kB)
Collecting ruff>=0.6 (from vela==0.1.0)
  Downloading ruff-0.15.21-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (26 kB)
Collecting anyio (from httpx>=0.27->vela==0.1.0)
  Downloading anyio-4.14.2-py3-none-any.whl.metadata (4.6 kB)
Collecting certifi (from httpx>=0.27->vela==0.1.0)
  Downloading certifi-2026.6.17-py3-none-any.whl.metadata (2.5 kB)
Collecting httpcore==1.* (from httpx>=0.27->vela==0.1.0)
  Using cached httpcore-1.0.9-py3-none-any.whl.metadata (21 kB)
Collecting idna (from httpx>=0.27->vela==0.1.0)
  Using cached idna-3.18-py3-none-any.whl.metadata (6.1 kB)
Collecting h11>=0.16 (from httpcore==1.*->httpx>=0.27->vela==0.1.0)
  Using cached h11-0.16.0-py3-none-any.whl.metadata (8.3 kB)
Collecting click<9.0.0,>=8.4.2 (from huggingface-hub>=0.27->vela==0.1.0)
  Downloading click-8.4.2-py3-none-any.whl.metadata (2.6 kB)
Collecting filelock>=3.10.0 (from huggingface-hub>=0.27->vela==0.1.0)
  Downloading filelock-3.29.7-py3-none-any.whl.metadata (2.0 kB)
Collecting fsspec>=2023.5.0 (from huggingface-hub>=0.27->vela==0.1.0)
  Downloading fsspec-2026.6.0-py3-none-any.whl.metadata (10 kB)
Collecting hf-xet<2.0.0,>=1.5.1 (from huggingface-hub>=0.27->vela==0.1.0)
  Using cached hf_xet-1.5.1-cp37-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl.metadata (4.9 kB)
Collecting packaging>=20.9 (from huggingface-hub>=0.27->vela==0.1.0)
  Using cached packaging-26.2-py3-none-any.whl.metadata (3.5 kB)
Collecting typing-extensions>=4.1.0 (from huggingface-hub>=0.27->vela==0.1.0)
  Downloading typing_extensions-4.16.0-py3-none-any.whl.metadata (3.3 kB)
Collecting mypy_extensions>=1.0.0 (from mypy>=1.8->vela==0.1.0)
  Using cached mypy_extensions-1.1.0-py3-none-any.whl.metadata (1.1 kB)
Collecting pathspec>=1.0.0 (from mypy>=1.8->vela==0.1.0)
  Using cached pathspec-1.1.1-py3-none-any.whl.metadata (14 kB)
Collecting librt>=0.12.0 (from mypy>=1.8->vela==0.1.0)
  Downloading librt-0.13.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (1.3 kB)
Collecting ast-serialize<1.0.0,>=0.6.0 (from mypy>=1.8->vela==0.1.0)
  Downloading ast_serialize-0.6.0-cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (1.3 kB)
Collecting annotated-types>=0.6.0 (from pydantic>=2.8->vela==0.1.0)
  Using cached annotated_types-0.7.0-py3-none-any.whl.metadata (15 kB)
Collecting pydantic-core==2.46.4 (from pydantic>=2.8->vela==0.1.0)
  Using cached pydantic_core-2.46.4-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (6.6 kB)
Collecting typing-inspection>=0.4.2 (from pydantic>=2.8->vela==0.1.0)
  Using cached typing_inspection-0.4.2-py3-none-any.whl.metadata (2.6 kB)
Collecting iniconfig>=1.0.1 (from pytest>=8.2->vela==0.1.0)
  Using cached iniconfig-2.3.0-py3-none-any.whl.metadata (2.5 kB)
Collecting pluggy<2,>=1.5 (from pytest>=8.2->vela==0.1.0)
  Using cached pluggy-1.6.0-py3-none-any.whl.metadata (4.8 kB)
Collecting pygments>=2.7.2 (from pytest>=8.2->vela==0.1.0)
  Using cached pygments-2.20.0-py3-none-any.whl.metadata (2.5 kB)
Collecting markdown-it-py>=2.2.0 (from rich>=13.7->vela==0.1.0)
  Using cached markdown_it_py-4.2.0-py3-none-any.whl.metadata (7.4 kB)
Collecting mdit-py-plugins (from textual<9,>=8.2->vela==0.1.0)
  Using cached mdit_py_plugins-0.6.1-py3-none-any.whl.metadata (2.9 kB)
Collecting platformdirs<5,>=3.6.0 (from textual<9,>=8.2->vela==0.1.0)
  Using cached platformdirs-4.10.0-py3-none-any.whl.metadata (5.5 kB)
Collecting shellingham>=1.3.0 (from typer>=0.12->vela==0.1.0)
  Using cached shellingham-1.5.4-py2.py3-none-any.whl.metadata (3.5 kB)
Collecting annotated-doc>=0.0.2 (from typer>=0.12->vela==0.1.0)
  Using cached annotated_doc-0.0.4-py3-none-any.whl.metadata (6.6 kB)
Collecting mdurl~=0.1 (from markdown-it-py>=2.2.0->rich>=13.7->vela==0.1.0)
  Using cached mdurl-0.1.2-py3-none-any.whl.metadata (1.6 kB)
Collecting linkify-it-py<3,>=1 (from markdown-it-py[linkify]>=2.1.0->textual<9,>=8.2->vela==0.1.0)
  Using cached linkify_it_py-2.1.0-py3-none-any.whl.metadata (8.5 kB)
Collecting uc-micro-py (from linkify-it-py<3,>=1->markdown-it-py[linkify]>=2.1.0->textual<9,>=8.2->vela==0.1.0)
  Using cached uc_micro_py-2.0.0-py3-none-any.whl.metadata (2.2 kB)
Using cached httpx-0.28.1-py3-none-any.whl (73 kB)
Using cached httpcore-1.0.9-py3-none-any.whl (78 kB)
Downloading huggingface_hub-1.23.0-py3-none-any.whl (770 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 770.3/770.3 kB 14.5 MB/s eta 0:00:00
Downloading mypy-2.2.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (15.2 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 15.2/15.2 MB 103.5 MB/s eta 0:00:00
Using cached psutil-7.2.2-cp36-abi3-manylinux2010_x86_64.manylinux_2_12_x86_64.manylinux_2_28_x86_64.whl (155 kB)
Using cached pydantic-2.13.4-py3-none-any.whl (472 kB)
Using cached pydantic_core-2.46.4-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (2.1 MB)
Downloading pytest-9.1.1-py3-none-any.whl (386 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 386.5/386.5 kB 22.5 MB/s eta 0:00:00
Using cached pytest_asyncio-1.4.0-py3-none-any.whl (16 kB)
Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)
Using cached pyyaml-6.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (807 kB)
Using cached rich-15.0.0-py3-none-any.whl (310 kB)
Downloading ruff-0.15.21-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (11.5 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 11.5/11.5 MB 121.7 MB/s eta 0:00:00
Downloading textual-8.2.8-py3-none-any.whl (731 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 731.4/731.4 kB 34.8 MB/s eta 0:00:00
Downloading tqdm-4.68.4-py3-none-any.whl (676 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 676.6/676.6 kB 34.3 MB/s eta 0:00:00
Downloading typer-0.26.8-py3-none-any.whl (122 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 122.6/122.6 kB 7.7 MB/s eta 0:00:00
Using cached annotated_doc-0.0.4-py3-none-any.whl (5.3 kB)
Using cached annotated_types-0.7.0-py3-none-any.whl (13 kB)
Downloading ast_serialize-0.6.0-cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (1.3 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 1.3/1.3 MB 50.9 MB/s eta 0:00:00
Downloading click-8.4.2-py3-none-any.whl (119 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 119.2/119.2 kB 7.6 MB/s eta 0:00:00
Downloading filelock-3.29.7-py3-none-any.whl (46 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 46.0/46.0 kB 2.6 MB/s eta 0:00:00
Downloading fsspec-2026.6.0-py3-none-any.whl (203 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 203.9/203.9 kB 12.6 MB/s eta 0:00:00
Using cached hf_xet-1.5.1-cp37-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl (4.5 MB)
Using cached iniconfig-2.3.0-py3-none-any.whl (7.5 kB)
Downloading librt-0.13.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (531 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 531.8/531.8 kB 28.8 MB/s eta 0:00:00
Using cached markdown_it_py-4.2.0-py3-none-any.whl (91 kB)
Using cached mypy_extensions-1.1.0-py3-none-any.whl (5.0 kB)
Using cached packaging-26.2-py3-none-any.whl (100 kB)
Using cached pathspec-1.1.1-py3-none-any.whl (57 kB)
Using cached platformdirs-4.10.0-py3-none-any.whl (22 kB)
Using cached pluggy-1.6.0-py3-none-any.whl (20 kB)
Using cached pygments-2.20.0-py3-none-any.whl (1.2 MB)
Using cached shellingham-1.5.4-py2.py3-none-any.whl (9.8 kB)
Downloading typing_extensions-4.16.0-py3-none-any.whl (45 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 45.6/45.6 kB 2.5 MB/s eta 0:00:00
Using cached typing_inspection-0.4.2-py3-none-any.whl (14 kB)
Downloading anyio-4.14.2-py3-none-any.whl (125 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 125.8/125.8 kB 7.9 MB/s eta 0:00:00
Using cached idna-3.18-py3-none-any.whl (65 kB)
Downloading certifi-2026.6.17-py3-none-any.whl (133 kB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 133.3/133.3 kB 8.6 MB/s eta 0:00:00
Using cached mdit_py_plugins-0.6.1-py3-none-any.whl (66 kB)
Using cached h11-0.16.0-py3-none-any.whl (37 kB)
Using cached linkify_it_py-2.1.0-py3-none-any.whl (19 kB)
Using cached mdurl-0.1.2-py3-none-any.whl (10.0 kB)
Using cached uc_micro_py-2.0.0-py3-none-any.whl (6.4 kB)
Building wheels for collected packages: vela
  Building wheel for vela (pyproject.toml): started
  Building wheel for vela (pyproject.toml): finished with status 'done'
  Created wheel for vela: filename=vela-0.1.0-py3-none-any.whl size=324306 sha256=9626aa74ae06915e2885b62b72ed440b79ccb5cfa8f3d43f61a0da5392addb89
  Stored in directory: /tmp/pip-ephem-wheel-cache-2s__qubh/wheels/36/e4/f9/900999049080c6a1ee0d176f2abbec5d4144d76b383b94ac0f
Successfully built vela
Installing collected packages: uc-micro-py, typing-extensions, tqdm, shellingham, ruff, pyyaml, pygments, psutil, pluggy, platformdirs, pathspec, packaging, mypy_extensions, mdurl, librt, iniconfig, idna, hf-xet, h11, fsspec, filelock, click, certifi, ast-serialize, annotated-types, annotated-doc, typing-inspection, pytest, pydantic-core, mypy, markdown-it-py, linkify-it-py, httpcore, anyio, rich, pytest-timeout, pytest-asyncio, pydantic, mdit-py-plugins, httpx, typer, textual, huggingface-hub, vela
Successfully installed annotated-doc-0.0.4 annotated-types-0.7.0 anyio-4.14.2 ast-serialize-0.6.0 certifi-2026.6.17 click-8.4.2 filelock-3.29.7 fsspec-2026.6.0 h11-0.16.0 hf-xet-1.5.1 httpcore-1.0.9 httpx-0.28.1 huggingface-hub-1.23.0 idna-3.18 iniconfig-2.3.0 librt-0.13.0 linkify-it-py-2.1.0 markdown-it-py-4.2.0 mdit-py-plugins-0.6.1 mdurl-0.1.2 mypy-2.2.0 mypy_extensions-1.1.0 packaging-26.2 pathspec-1.1.1 platformdirs-4.10.0 pluggy-1.6.0 psutil-7.2.2 pydantic-2.13.4 pydantic-core-2.46.4 pygments-2.20.0 pytest-9.1.1 pytest-asyncio-1.4.0 pytest-timeout-2.4.0 pyyaml-6.0.3 rich-15.0.0 ruff-0.15.21 shellingham-1.5.4 textual-8.2.8 tqdm-4.68.4 typer-0.26.8 typing-extensions-4.16.0 typing-inspection-0.4.2 uc-micro-py-2.0.0 vela-0.1.0
== Remote agent restart ==
running pid=1174060 socket=/tank/venvs/lab-tui-remediation-de9b0a1/agent-runtime/agent.sock
== Remote host ==
oxcart
GPU unavailable=False note=
GPU 0 NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition GPU-aebb15f2-0592-40da-7d2e-77d70241825c mem=2/97887MiB util=0%
vllm not found on PATH; no-GPU package checks will still run
All checks passed!
........................................................................ [ 44%]
........................................................................ [ 88%]
...................                                                      [100%]
163 passed in 1.29s
fake-child	fake/model
qwen3-32b-fp8-62001	/tank/trt/models/Qwen3-32B-FP8
qwen36-27b-bf16-rp6000-blackbird	Qwen/Qwen3.6-27B
qwen36-27b-fp8-kvfp8-rp6000-blackbird	Qwen/Qwen3.6-27B-FP8
real-vllm-example	mistralai/Mistral-7B-Instruct-v0.3
tiny-random-llama-detached-blackbird	hf-internal-testing/tiny-random-LlamaForCausalLM
cwd=/tmp/vela-remote-validation.91cKX1/checkout
PYTHONUNBUFFERED=1
./scripts/fake_vllm_child.py serve fake/model --served-model-name fake-model --host 127.0.0.1 --port 8765
== Daemon restart live-run survival ==
running pid=1174477 socket=/tank/venvs/lab-tui-remediation-de9b0a1/agent-runtime/agent.sock
DAEMON_RESTART_LIVE_RUN_OK run_id=daemon-restart-1b4a7698330a4e429c5f884d1b3b5cd7 port=37853 url=http://127.0.0.1:37853 returncode=0
== Disconnect/reconnect stream resume ==
DISCONNECT_RECONNECT_RESUME_OK run_id=disconnect-reconnect-3df7fff7d0d14d1b81667d88419b0d33 first_seq=1 resume_inode=1847861 resume_offset=34 url=http://127.0.0.1:53499 returncode=0
```

## Result

- Completed: `2026-07-13T07:54:47Z`
- Exit status: `0`
