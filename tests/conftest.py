"""Fixture comuni. Ogni test gira con la rete BLOCCATA (eccetto loopback/AF_UNIX)."""

from __future__ import annotations

import socket

import pytest

_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo
LOCAL = {"127.0.0.1", "::1", "localhost", "testserver"}


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def guarded_connect(self, address):
        if isinstance(address, tuple) and address and address[0] not in LOCAL:
            raise NetworkBlocked(f"rete bloccata nei test: {address}")
        return _real_connect(self, address)

    def guarded_getaddrinfo(host, *a, **k):
        if host not in LOCAL and host is not None:
            raise NetworkBlocked(f"DNS bloccato nei test: {host}")
        return _real_getaddrinfo(host, *a, **k)

    # niente proxy: altrimenti il traffico passerebbe da un proxy locale (loopback) e uscirebbe comunque
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
