from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def deny_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail every in-process test that attempts DNS lookup or socket connection."""

    def blocked(*args, **kwargs):
        raise AssertionError("tests must not access the network")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
