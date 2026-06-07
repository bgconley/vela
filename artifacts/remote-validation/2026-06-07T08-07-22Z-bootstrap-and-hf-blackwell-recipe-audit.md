# Bootstrap And HF Blackwell Recipe Audit - 2026-06-07

## Scope

This artifact records two non-GPU validation steps from the current v1 close-out:

- `vela targets bootstrap <name> --host ... --install --build ...` acceptance through the fake-SSH harness required by Track B.
- Hugging Face model metadata checked against the local Blackbird Qwen3.6 deployment recipes.

No Blackwell launch shape was inferred from Hugging Face metadata. The local deployment scripts, checked-in Vela configs, and real Blackbird backend-evidence artifacts remain the authority for the vLLM image digest, SM120/FlashInfer/CUTLASS-sensitive runtime shape, FlashAttention/backend selection, cache mounts, and FP8/BF16 KV-memory settings.

## Bootstrap Acceptance

Command shape:

```bash
tmpdir="$(mktemp -d)"
mkdir -p "$tmpdir/bin"
PYTHONPATH=src python - "$tmpdir/bin/ssh" <<'PY'
from pathlib import Path
import sys
from tests.fakes.fake_ssh import write_fake_ssh_runtime
path = Path(sys.argv[1])
write_fake_ssh_runtime(path)
path.chmod(0o755)
PY
PATH="$tmpdir/bin:$PATH" \
XDG_CONFIG_HOME="$tmpdir/config" \
FAKE_SSH_VELA_PRESENT=0 \
FAKE_SSH_INSTALLED_MARKER="$tmpdir/installed" \
FAKE_SSH_VELA_VERSION="$(PYTHONPATH=src python -c 'import vela; print(vela.__version__)')" \
PYTHONPATH=src python -m vela targets bootstrap blackbird \
  --host bgconley@fake \
  --install \
  --build vllm==0.11.2
```

Observed output:

```text
OK	ssh	reachable
OK	agent	installed /home/bgconley/.local/share/vela/venv/bin/vela
OK	target	wrote <tmp>/config/vela/targets.yaml
OK	handshake	agent=0.1.0	protocol=None
bootstrapped target blackbird	<tmp>/config/vela/targets.yaml
Installing build
DONE	9c6edddd919b4037b1939080c9bc1cf1	build ready
BOOTSTRAP_ACCEPTANCE_EXIT=0
FAKE_INSTALL_MARKER=installed
```

Reason this used fake SSH instead of re-provisioning P620/Blackbird: the installer intentionally targets the canonical remote path `~/.local/share/vela/venv/bin/vela`; exercising that against production hosts would rewrite host-managed install state. The fake-SSH harness is the Track B behavioral harness for install/discovery/bootstrap acceptance.

## Hugging Face Metadata Check

Live Hub metadata checked:

- `Qwen/Qwen3.6-27B-FP8`: task `image-text-to-text`, library `transformers`, model class `AutoModelForImageTextToText`, architecture tag `qwen3_5`, parameter count about 27.8B, FP8 tag present, Apache-2.0 license.
- `Qwen/Qwen3.6-27B`: task `image-text-to-text`, library `transformers`, model class `AutoModelForImageTextToText`, architecture tag `qwen3_5`, parameter count about 27.8B, Apache-2.0 license.
- Both `config.json` files report `text_config.max_position_embeddings: 262144`.
- The FP8 `config.json` includes FP8 quantization metadata with dynamic activation scheme and e4m3 format.
- Both repos expose Qwen3/VL processor-tokenizer metadata; Vela's Blackbird recipes intentionally serve them with `--language-model-only` and `--limit-mm-per-prompt {"image":0,"video":0}` for the FP8 lane.

Interpretation:

- HF metadata supports model identity, revision/tokenizer/config awareness, gating/cache checks, and broad engine suggestions such as context length and FP8 tagging.
- HF metadata does not specify the validated Blackwell vLLM container digest, CUDA 13/Torch stack, `FLASHINFER_CUDA_ARCH_LIST=12.0f`, Cutlass FP8 kernel proof, FlashInfer attention proof, cache layout, or FP8/BF16 KV-cache memory shape.
- The deployment composer must therefore use local lab recipes for known Blackbird/P620 Blackwell deployments and refuse recipe-less Blackwell FP8 Docker composition instead of inventing a runtime from HF metadata alone.

Relevant sources:

- Hugging Face: https://hf.co/Qwen/Qwen3.6-27B-FP8
- Hugging Face: https://hf.co/Qwen/Qwen3.6-27B
- Local recipe provenance: `/Users/brennanconley/vibecode/infx/qwen36-27b-test/start-qwen36-27b-fp8-rp6000-blackbird.sh`
- Local recipe provenance: `/Users/brennanconley/vibecode/infx/qwen36-27b-test/start-qwen36-bf16-rp6000-blackbird.sh`
- Checked-in Vela recipes: `configs/qwen36-27b-fp8-kvfp8-rp6000-blackbird.yaml`
- Checked-in Vela recipes: `configs/qwen36-27b-bf16-rp6000-blackbird.yaml`
