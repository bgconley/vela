from __future__ import annotations

from pathlib import Path


def write_fake_docker_runtime(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json, os, sys",
                "args = sys.argv[1:]",
                "image_digest = os.environ.get('FAKE_DOCKER_IMAGE_DIGEST', 'sha256:image')",
                "log = os.environ.get('FAKE_DOCKER_COMMAND_LOG')",
                "if log:",
                "    with open(log, 'a', encoding='utf-8') as file:",
                "        file.write(' '.join(args) + '\\n')",
                "if args[:2] == ['image', 'inspect']:",
                "    if os.environ.get('FAKE_DOCKER_IMAGE_MISSING') == '1':",
                "        print('Error: No such image: ' + args[-1], file=sys.stderr)",
                "        raise SystemExit(1)",
                "    payload = [{",
                "        'Id': image_digest,",
                "        'RepoDigests': ['vllm/vllm-openai@' + image_digest],",
                "    }]",
                "    print(json.dumps(payload))",
                "    raise SystemExit(0)",
                "if args[:1] == ['pull']:",
                "    if os.environ.get('FAKE_DOCKER_PULL_FAIL') == '1':",
                "        print('pull failed for ' + args[-1], file=sys.stderr)",
                "        raise SystemExit(1)",
                "    print('pulled ' + args[-1])",
                "    raise SystemExit(0)",
                "if args[:2] == ['run', '-d']:",
                "    print('container-123')",
                "    raise SystemExit(0)",
                "if args[:1] == ['ps']:",
                "    print(os.environ.get('FAKE_DOCKER_PS', ''))",
                "    raise SystemExit(0)",
                "if args[:2] == ['logs', '-f']:",
                "    print('INFO Uvicorn running on http://0.0.0.0:8000', flush=True)",
                "    raise SystemExit(0)",
                "if args[:1] == ['wait']:",
                "    print('0')",
                "    raise SystemExit(0)",
                "if args[:1] == ['inspect']:",
                "    payload = [{",
                "        'Id': 'container-123',",
                "        'Name': '/vela-qwen',",
                "        'Image': image_digest,",
                "        'Config': {'Image': 'vllm/vllm-openai@' + image_digest},",
                "        'RepoDigests': ['vllm/vllm-openai@' + image_digest],",
                "    }]",
                "    print(json.dumps(payload))",
                "    raise SystemExit(0)",
                "if args[:1] in (['stop'], ['kill']):",
                "    raise SystemExit(0)",
                "if args[:1] == ['rm']:",
                "    raise SystemExit(0)",
                "raise SystemExit(f'unexpected docker args: {args}')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
