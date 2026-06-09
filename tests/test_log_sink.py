from __future__ import annotations

import errno
import os
from pathlib import Path

from vela.engine.log_sink import (
    LogSink,
    OSErrorByteReader,
    display_level_for_line,
    is_pty_eof,
)
from vela.engine.redaction import scrub_text


def test_display_level_dims_known_benign_shutdown_noise() -> None:
    # The screenshot-#7 NCCL/torch shutdown line is benign — classify it dim so
    # the log view de-emphasizes it instead of styling it as a warning/error.
    benign = (
        "[rank0]:[W1231 23:59:59.000 ProcessGroupNCCL.cpp:1234] Warning: "
        "destroy_process_group() was not called before program exit"
    )
    assert display_level_for_line(benign) == "BENIGN"
    # Real warnings/errors keep their level (not downgraded).
    assert display_level_for_line("WARNING something is off") == "WARNING"
    assert display_level_for_line("ERROR boom") == "ERROR"
    # Plain / useful lines stay unclassified.
    assert display_level_for_line("Uvicorn running on http://0.0.0.0:8000") is None


def test_splits_carriage_return_and_newline_and_persists_only_committed(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    records: list[tuple[str, str]] = []
    sink = LogSink(path, secrets=[], emit=lambda record: records.append((record.kind, record.text)))

    sink.feed(b"progress 10%\rINFO ready\n")
    sink.close()

    assert records == [("transient", "progress 10%"), ("committed", "INFO ready")]
    assert path.read_text(encoding="utf-8") == "INFO ready\n"


def test_terminal_carriage_return_emits_transient_without_next_chunk(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    records: list[tuple[str, str]] = []
    sink = LogSink(path, secrets=[], emit=lambda record: records.append((record.kind, record.text)))

    sink.feed(b"Loading checkpoint shards: 10%\r")

    assert records == [("transient", "Loading checkpoint shards: 10%")]
    assert path.read_text(encoding="utf-8") == ""
    sink.close()
    assert path.read_text(encoding="utf-8") == ""


def test_split_crlf_after_terminal_carriage_return_commits_line(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    records: list[tuple[str, str]] = []
    sink = LogSink(path, secrets=[], emit=lambda record: records.append((record.kind, record.text)))

    sink.feed(b"INFO ready\r")
    sink.feed(b"\n")
    sink.close()

    assert records == [("transient", "INFO ready"), ("committed", "INFO ready")]
    assert path.read_text(encoding="utf-8") == "INFO ready\n"


def test_multibyte_utf8_split_across_chunks_decodes_correctly(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    lines: list[str] = []
    sink = LogSink(path, secrets=[], emit=lambda record: lines.append(record.text))

    payload = "INFO snowman ☃\n".encode()
    sink.feed(payload[:14])
    sink.feed(payload[14:])
    sink.close()

    assert lines == ["INFO snowman ☃"]
    assert path.read_text(encoding="utf-8") == "INFO snowman ☃\n"


def test_pty_eio_is_treated_as_eof() -> None:
    assert is_pty_eof(OSError(errno.EIO, "Input/output error"))
    assert not is_pty_eof(OSError(errno.EBADF, "bad fd"))


def test_bounded_partial_line_truncation(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    lines: list[str] = []
    sink = LogSink(path, secrets=[], emit=lambda record: lines.append(record.text), max_pending=8)

    sink.feed(b"abcdefghijk")
    sink.close()

    assert lines == ["abcdefgh […line truncated at 8 bytes…]", "ijk"]
    assert "truncated" in path.read_text(encoding="utf-8")


def test_bounded_partial_line_repeatedly_flushes_long_overflow(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    lines: list[str] = []
    sink = LogSink(path, secrets=[], emit=lambda record: lines.append(record.text), max_pending=4)

    sink.feed(b"abcdefghijk")
    sink.close()

    assert lines == [
        "abcd […line truncated at 4 bytes…]",
        "efgh […line truncated at 4 bytes…]",
        "ijk",
    ]


def test_api_key_hf_token_bearer_and_sk_tokens_scrubbed_for_ui_and_file(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    lines: list[str] = []
    sink = LogSink(
        path,
        secrets=["literal-api-key", "hf_literal"],
        emit=lambda record: lines.append(record.text),
    )

    sink.feed(
        b"Authorization: Bearer abc123 literal-api-key hf_literal sk-abcdefghijklmnopqrstuvwxyz\n"
    )
    sink.close()

    joined = "\n".join(lines) + path.read_text(encoding="utf-8")
    assert "literal-api-key" not in joined
    assert "hf_literal" not in joined
    assert "abc123" not in joined
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in joined
    assert "Bearer ••••" in joined
    assert "••••" in joined


def test_generic_sk_token_scrubbing_masks_non_whitespace_suffix(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    lines: list[str] = []
    sink = LogSink(path, secrets=[], emit=lambda record: lines.append(record.text))

    sink.feed(b"INFO leaked sk-live.secret/with-symbols?abc=123 in output\n")
    sink.close()

    joined = "\n".join(lines) + path.read_text(encoding="utf-8")
    assert "sk-live.secret/with-symbols?abc=123" not in joined
    assert ".secret/with-symbols" not in joined
    assert "INFO leaked •••• in output" in joined


def test_generic_token_scrubbing_preserves_structural_delimiters() -> None:
    assert (
        scrub_text('{"token":"hf_live_token","api_key":"sk-live.secret/with?x=1"}')
        == '{"token":"••••","api_key":"••••"}'
    )
    assert scrub_text("export HF_TOKEN=hf_shell_token; echo ok") == (
        "export HF_TOKEN=••••; echo ok"
    )


def test_ansi_colored_vllm_line_is_sanitized_and_classified(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    records: list[tuple[str, str | None]] = []
    sink = LogSink(
        path,
        secrets=[],
        emit=lambda record: records.append((record.text, record.level)),
    )

    sink.feed(
        b"\x1b[0;36m(APIServer pid=395468)\x1b[0;0m "
        b"\x1b[32mINFO\x1b[0m:     Started server process "
        b"[\x1b[36m395468\x1b[0m]\n"
    )
    sink.close()

    durable_text = path.read_text(encoding="utf-8")
    assert "\x1b" not in records[0][0]
    assert "\x1b" not in durable_text
    assert records == [
        ("(APIServer pid=395468) INFO:     Started server process [395468]", "INFO")
    ]
    assert durable_text == "(APIServer pid=395468) INFO:     Started server process [395468]\n"


def test_durable_file_mode_is_0600(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    sink = LogSink(path, secrets=[])
    sink.feed(b"INFO hi\n")
    sink.close()

    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_oserror_byte_reader_treats_eio_as_empty() -> None:
    reader = OSErrorByteReader(OSError(errno.EIO, "done"))

    assert reader.read() == b""
