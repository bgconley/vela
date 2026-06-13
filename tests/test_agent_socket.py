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
    from vela.agent import socket as socket_module

    monkeypatch.setattr(socket_module, "_peer_uid_from_socket", lambda _sock: os.getuid())

    socket_module.verify_same_user_peer(FakeWriter(object()))


def test_same_user_peer_check_rejects_mismatched_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vela.agent import socket as socket_module

    monkeypatch.setattr(
        socket_module,
        "_peer_uid_from_socket",
        lambda _sock: os.getuid() + 1,
    )

    with pytest.raises(PermissionError, match="peer uid"):
        socket_module.verify_same_user_peer(FakeWriter(object()))


def test_same_user_peer_check_fails_closed_without_creds_or_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vela.agent import socket as socket_module

    monkeypatch.delenv("VELA_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("VELA_AGENT_TOKEN_FILE", raising=False)
    monkeypatch.setattr(socket_module, "_peer_uid_from_socket", lambda _sock: None)

    with pytest.raises(PermissionError, match="peer credentials"):
        socket_module.verify_same_user_peer(FakeWriter(object()))


def test_same_user_peer_check_allows_unverifiable_peer_when_token_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vela.agent import socket as socket_module
    from vela.agent.auth import generate_agent_token

    monkeypatch.setenv("VELA_AGENT_TOKEN", generate_agent_token())
    monkeypatch.setattr(socket_module, "_peer_uid_from_socket", lambda _sock: None)

    # With a capability token as the auth fallback, an unverifiable peer is
    # allowed (the token gate enforces auth); without a token it fails closed.
    socket_module.verify_same_user_peer(FakeWriter(object()))
