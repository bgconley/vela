from __future__ import annotations

import os

import pytest


class FakeWriter:
    def __init__(self, sock: object) -> None:
        self._sock = sock

    def get_extra_info(self, name: str):
        if name == "socket":
            return self._sock
        return None


def test_same_user_peer_check_accepts_current_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vllm_loader.agent import socket as socket_module

    monkeypatch.setattr(socket_module, "_peer_uid_from_socket", lambda _sock: os.getuid())

    socket_module.verify_same_user_peer(FakeWriter(object()))


def test_same_user_peer_check_rejects_mismatched_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vllm_loader.agent import socket as socket_module

    monkeypatch.setattr(
        socket_module,
        "_peer_uid_from_socket",
        lambda _sock: os.getuid() + 1,
    )

    with pytest.raises(PermissionError, match="peer uid"):
        socket_module.verify_same_user_peer(FakeWriter(object()))
