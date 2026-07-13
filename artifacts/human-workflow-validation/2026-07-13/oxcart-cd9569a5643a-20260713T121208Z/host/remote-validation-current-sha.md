# Vela Remote Validation

- Started: `2026-07-13T13:34:25Z`
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
- SSH command: `ssh -o BatchMode=yes -o ConnectTimeout=15 bgconley@10.25.0.50 env VELA_REMOTE_BRANCH=remediate/2026-07-09-review VELA_REMOTE_EXPECTED_SHA=cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7 VELA_REMOTE_AGENT_RUNTIME_DIR=/tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-agent-runtime VELA_REMOTE_PYTEST_ARGS=-q\\\ tests/test_remote_workflow.py\\\ tests/test_transport_factory.py\\\ tests/test_targets.py bash -s -- /home/bgconley/repos/lab-tui 1800 auto /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-venv `

## Output

```text
== Remote git revision (remediate/2026-07-09-review) ==
HEAD is now at cd9569a fix: render deployment data as literal text
Preparing worktree (detached HEAD cd9569a)
REMOTE_REVISION_OK expected=cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7 actual=cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7 branch=remediate/2026-07-09-review source=owned-worktree
Processing /tmp/vela-remote-validation.nV6JuN/checkout
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
  Getting requirements to build wheel: started
  Getting requirements to build wheel: finished with status 'done'
  Preparing metadata (pyproject.toml): started
  Preparing metadata (pyproject.toml): finished with status 'done'
Collecting httpx>=0.27 (from vela==0.1.0)
  Using cached httpx-0.28.1-py3-none-any.whl.metadata (7.1 kB)
Collecting huggingface-hub>=0.27 (from vela==0.1.0)
  Using cached huggingface_hub-1.23.0-py3-none-any.whl.metadata (14 kB)
Collecting psutil>=5.9 (from vela==0.1.0)
  Using cached psutil-7.2.2-cp36-abi3-manylinux2010_x86_64.manylinux_2_12_x86_64.manylinux_2_28_x86_64.whl.metadata (22 kB)
Collecting pydantic>=2.8 (from vela==0.1.0)
  Using cached pydantic-2.13.4-py3-none-any.whl.metadata (109 kB)
Collecting pyyaml>=6.0 (from vela==0.1.0)
  Using cached pyyaml-6.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (2.4 kB)
Collecting rich>=13.7 (from vela==0.1.0)
  Using cached rich-15.0.0-py3-none-any.whl.metadata (18 kB)
Collecting textual<9,>=8.2 (from vela==0.1.0)
  Using cached textual-8.2.8-py3-none-any.whl.metadata (9.1 kB)
Collecting tqdm>=4.66 (from vela==0.1.0)
  Using cached tqdm-4.68.4-py3-none-any.whl.metadata (57 kB)
Collecting typer>=0.12 (from vela==0.1.0)
  Using cached typer-0.26.8-py3-none-any.whl.metadata (15 kB)
Collecting mypy>=1.8 (from vela==0.1.0)
  Downloading mypy-2.3.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (2.4 kB)
Collecting pytest-asyncio>=0.24 (from vela==0.1.0)
  Using cached pytest_asyncio-1.4.0-py3-none-any.whl.metadata (4.1 kB)
Collecting pytest-timeout>=2.3 (from vela==0.1.0)
  Using cached pytest_timeout-2.4.0-py3-none-any.whl.metadata (20 kB)
Collecting pytest>=8.2 (from vela==0.1.0)
  Using cached pytest-9.1.1-py3-none-any.whl.metadata (7.6 kB)
Collecting ruff>=0.6 (from vela==0.1.0)
  Using cached ruff-0.15.21-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (26 kB)
Collecting anyio (from httpx>=0.27->vela==0.1.0)
  Using cached anyio-4.14.2-py3-none-any.whl.metadata (4.6 kB)
Collecting certifi (from httpx>=0.27->vela==0.1.0)
  Using cached certifi-2026.6.17-py3-none-any.whl.metadata (2.5 kB)
Collecting httpcore==1.* (from httpx>=0.27->vela==0.1.0)
  Using cached httpcore-1.0.9-py3-none-any.whl.metadata (21 kB)
Collecting idna (from httpx>=0.27->vela==0.1.0)
  Using cached idna-3.18-py3-none-any.whl.metadata (6.1 kB)
Collecting h11>=0.16 (from httpcore==1.*->httpx>=0.27->vela==0.1.0)
  Using cached h11-0.16.0-py3-none-any.whl.metadata (8.3 kB)
Collecting click<9.0.0,>=8.4.2 (from huggingface-hub>=0.27->vela==0.1.0)
  Using cached click-8.4.2-py3-none-any.whl.metadata (2.6 kB)
Collecting filelock>=3.10.0 (from huggingface-hub>=0.27->vela==0.1.0)
  Using cached filelock-3.29.7-py3-none-any.whl.metadata (2.0 kB)
Collecting fsspec>=2023.5.0 (from huggingface-hub>=0.27->vela==0.1.0)
  Using cached fsspec-2026.6.0-py3-none-any.whl.metadata (10 kB)
Collecting hf-xet<2.0.0,>=1.5.1 (from huggingface-hub>=0.27->vela==0.1.0)
  Using cached hf_xet-1.5.1-cp37-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl.metadata (4.9 kB)
Collecting packaging>=20.9 (from huggingface-hub>=0.27->vela==0.1.0)
  Using cached packaging-26.2-py3-none-any.whl.metadata (3.5 kB)
Collecting typing-extensions>=4.1.0 (from huggingface-hub>=0.27->vela==0.1.0)
  Using cached typing_extensions-4.16.0-py3-none-any.whl.metadata (3.3 kB)
Collecting mypy_extensions>=1.0.0 (from mypy>=1.8->vela==0.1.0)
  Using cached mypy_extensions-1.1.0-py3-none-any.whl.metadata (1.1 kB)
Collecting pathspec>=1.0.0 (from mypy>=1.8->vela==0.1.0)
  Using cached pathspec-1.1.1-py3-none-any.whl.metadata (14 kB)
Collecting librt>=0.13.0 (from mypy>=1.8->vela==0.1.0)
  Using cached librt-0.13.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl.metadata (1.3 kB)
Collecting ast-serialize<1.0.0,>=0.6.0 (from mypy>=1.8->vela==0.1.0)
  Using cached ast_serialize-0.6.0-cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (1.3 kB)
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
Using cached huggingface_hub-1.23.0-py3-none-any.whl (770 kB)
Downloading mypy-2.3.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (15.3 MB)
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 15.3/15.3 MB 97.8 MB/s eta 0:00:00
Using cached psutil-7.2.2-cp36-abi3-manylinux2010_x86_64.manylinux_2_12_x86_64.manylinux_2_28_x86_64.whl (155 kB)
Using cached pydantic-2.13.4-py3-none-any.whl (472 kB)
Using cached pydantic_core-2.46.4-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (2.1 MB)
Using cached pytest-9.1.1-py3-none-any.whl (386 kB)
Using cached pytest_asyncio-1.4.0-py3-none-any.whl (16 kB)
Using cached pytest_timeout-2.4.0-py3-none-any.whl (14 kB)
Using cached pyyaml-6.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (807 kB)
Using cached rich-15.0.0-py3-none-any.whl (310 kB)
Using cached ruff-0.15.21-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (11.5 MB)
Using cached textual-8.2.8-py3-none-any.whl (731 kB)
Using cached tqdm-4.68.4-py3-none-any.whl (676 kB)
Using cached typer-0.26.8-py3-none-any.whl (122 kB)
Using cached annotated_doc-0.0.4-py3-none-any.whl (5.3 kB)
Using cached annotated_types-0.7.0-py3-none-any.whl (13 kB)
Using cached ast_serialize-0.6.0-cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (1.3 MB)
Using cached click-8.4.2-py3-none-any.whl (119 kB)
Using cached filelock-3.29.7-py3-none-any.whl (46 kB)
Using cached fsspec-2026.6.0-py3-none-any.whl (203 kB)
Using cached hf_xet-1.5.1-cp37-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl (4.5 MB)
Using cached iniconfig-2.3.0-py3-none-any.whl (7.5 kB)
Using cached librt-0.13.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl (531 kB)
Using cached markdown_it_py-4.2.0-py3-none-any.whl (91 kB)
Using cached mypy_extensions-1.1.0-py3-none-any.whl (5.0 kB)
Using cached packaging-26.2-py3-none-any.whl (100 kB)
Using cached pathspec-1.1.1-py3-none-any.whl (57 kB)
Using cached platformdirs-4.10.0-py3-none-any.whl (22 kB)
Using cached pluggy-1.6.0-py3-none-any.whl (20 kB)
Using cached pygments-2.20.0-py3-none-any.whl (1.2 MB)
Using cached shellingham-1.5.4-py2.py3-none-any.whl (9.8 kB)
Using cached typing_extensions-4.16.0-py3-none-any.whl (45 kB)
Using cached typing_inspection-0.4.2-py3-none-any.whl (14 kB)
Using cached anyio-4.14.2-py3-none-any.whl (125 kB)
Using cached idna-3.18-py3-none-any.whl (65 kB)
Using cached certifi-2026.6.17-py3-none-any.whl (133 kB)
Using cached mdit_py_plugins-0.6.1-py3-none-any.whl (66 kB)
Using cached h11-0.16.0-py3-none-any.whl (37 kB)
Using cached linkify_it_py-2.1.0-py3-none-any.whl (19 kB)
Using cached mdurl-0.1.2-py3-none-any.whl (10.0 kB)
Using cached uc_micro_py-2.0.0-py3-none-any.whl (6.4 kB)
Building wheels for collected packages: vela
  Building wheel for vela (pyproject.toml): started
  Building wheel for vela (pyproject.toml): finished with status 'done'
  Created wheel for vela: filename=vela-0.1.0-py3-none-any.whl size=340958 sha256=41763f0a6c1cf84b2d74135ddf819b527b7e1bd92d8fa8fe3a708404328525e6
  Stored in directory: /tmp/pip-ephem-wheel-cache-vyghb9wf/wheels/69/bb/b7/deb60e85421e77caa76f6632b4a75db60f301ecb53d84feef2
Successfully built vela
Installing collected packages: uc-micro-py, typing-extensions, tqdm, shellingham, ruff, pyyaml, pygments, psutil, pluggy, platformdirs, pathspec, packaging, mypy_extensions, mdurl, librt, iniconfig, idna, hf-xet, h11, fsspec, filelock, click, certifi, ast-serialize, annotated-types, annotated-doc, typing-inspection, pytest, pydantic-core, mypy, markdown-it-py, linkify-it-py, httpcore, anyio, rich, pytest-timeout, pytest-asyncio, pydantic, mdit-py-plugins, httpx, typer, textual, huggingface-hub, vela
Successfully installed annotated-doc-0.0.4 annotated-types-0.7.0 anyio-4.14.2 ast-serialize-0.6.0 certifi-2026.6.17 click-8.4.2 filelock-3.29.7 fsspec-2026.6.0 h11-0.16.0 hf-xet-1.5.1 httpcore-1.0.9 httpx-0.28.1 huggingface-hub-1.23.0 idna-3.18 iniconfig-2.3.0 librt-0.13.0 linkify-it-py-2.1.0 markdown-it-py-4.2.0 mdit-py-plugins-0.6.1 mdurl-0.1.2 mypy-2.3.0 mypy_extensions-1.1.0 packaging-26.2 pathspec-1.1.1 platformdirs-4.10.0 pluggy-1.6.0 psutil-7.2.2 pydantic-2.13.4 pydantic-core-2.46.4 pygments-2.20.0 pytest-9.1.1 pytest-asyncio-1.4.0 pytest-timeout-2.4.0 pyyaml-6.0.3 rich-15.0.0 ruff-0.15.21 shellingham-1.5.4 textual-8.2.8 tqdm-4.68.4 typer-0.26.8 typing-extensions-4.16.0 typing-inspection-0.4.2 uc-micro-py-2.0.0 vela-0.1.0
== Remote agent restart ==
start-failed socket=/tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-agent-runtime/agent.sock
  agent log: /tank/work/validation/vela-oxcart-pilot-oxcart-cd9569a5643a-20260713T121208Z/remote-lane-agent-runtime/agent-start.err
```

## Result

- Completed: `2026-07-13T13:34:43Z`
- Exit status: `1`
