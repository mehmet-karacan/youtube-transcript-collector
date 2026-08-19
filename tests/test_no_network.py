from __future__ import annotations

import socket

import pytest


def test_global_test_guard_blocks_dns_and_socket_access() -> None:
    with pytest.raises(AssertionError, match="must not access the network"):
        socket.getaddrinfo("example.invalid", 443)
    with pytest.raises(AssertionError, match="must not access the network"):
        socket.create_connection(("example.invalid", 443))
