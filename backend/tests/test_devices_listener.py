from __future__ import annotations

import errno
import logging
import os
import socket
import stat
from types import SimpleNamespace

import pytest

from infra import devices_listener
from infra.devices_listener import DevicesListener, ensure_host_key, lan_addresses, local_hostname


def test_host_key_is_created_under_state_root_with_private_modes(tmp_path):
    key_path, cert_path = ensure_host_key(tmp_path)

    assert key_path == tmp_path / "devices" / "host-key.pem"
    assert cert_path == tmp_path / "devices" / "host-cert.pem"
    assert stat.S_IMODE((tmp_path / "devices").stat().st_mode) == 0o700
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cert_path.stat().st_mode) == 0o600


def test_existing_key_with_unsafe_mode_is_refused(tmp_path):
    key_path, _ = ensure_host_key(tmp_path)
    os.chmod(key_path, 0o644)

    with pytest.raises(RuntimeError, match="unsafe"):
        ensure_host_key(tmp_path)


def test_address_hints_include_only_listener_reachable_lan_addresses():
    interfaces = {
        "en0": [
            SimpleNamespace(family=socket.AF_INET, address="192.168.1.20"),
            SimpleNamespace(family=socket.AF_INET, address="8.8.8.8"),
            SimpleNamespace(family=socket.AF_INET6, address="fd00::20"),
            SimpleNamespace(family=socket.AF_INET6, address="fe80::20%en0"),
        ],
        "lo0": [SimpleNamespace(family=socket.AF_INET, address="127.0.0.1")],
        "utun2": [SimpleNamespace(family=socket.AF_INET, address="10.0.0.4")],
        "tailscale0": [SimpleNamespace(family=socket.AF_INET, address="100.64.0.4")],
        "bridge0": [SimpleNamespace(family=socket.AF_INET, address="192.168.1.21")],
        "vpn0": [SimpleNamespace(family=socket.AF_INET6, address="fd00::21")],
    }

    assert lan_addresses(include_link_local=True, interface_addrs=interfaces) == [
        "192.168.1.20",
        "fd00::20",
        "fe80::20%en0",
    ]
    assert lan_addresses(include_link_local=False, interface_addrs=interfaces) == [
        "192.168.1.20",
        "fd00::20",
    ]


def test_local_hostname_is_a_lowercase_mdns_name(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "Studio.Local.")

    assert local_hostname() == "studio.local"


class _FakeSocket:
    def __init__(self, bind_error: int | OSError | None = None):
        self.bind_error = bind_error
        self.options = []
        self.bound = None
        self.listened = None
        self.blocking = None
        self.closed = False

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))

    def bind(self, address):
        self.bound = address
        if isinstance(self.bind_error, OSError):
            raise self.bind_error
        if self.bind_error is not None:
            raise OSError(self.bind_error, os.strerror(self.bind_error))

    def listen(self, backlog):
        self.listened = backlog

    def setblocking(self, enabled):
        self.blocking = enabled

    def close(self):
        self.closed = True


def _socket_factory(bind_errors):
    sockets = []

    def create(family, sock_type):
        assert (family, sock_type) == (socket.AF_INET6, socket.SOCK_STREAM)
        sock = _FakeSocket(bind_errors[len(sockets)])
        sockets.append(sock)
        return sock

    return create, sockets


def test_prebind_retries_only_address_in_use_and_logs_the_fallback(monkeypatch, caplog):
    create, sockets = _socket_factory([errno.EADDRINUSE, None])
    monkeypatch.setattr(devices_listener.socket, "socket", create)

    with caplog.at_level(logging.INFO, logger="infra.devices_listener"):
        sock, port, error = devices_listener._prebind()

    assert (sock, port, error) == (sockets[1], 47902, None)
    assert sockets[0].bound == ("::", 47901)
    assert sockets[0].closed is True
    assert sockets[1].bound == ("::", 47902)
    assert (socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0) in sockets[1].options
    assert sockets[1].listened == socket.SOMAXCONN
    assert sockets[1].blocking is False
    assert "fallback port 47902" in caplog.text


def test_prebind_restarts_from_the_default_port_each_time(monkeypatch):
    create, sockets = _socket_factory([errno.EADDRINUSE, None, errno.EADDRINUSE, None])
    monkeypatch.setattr(devices_listener.socket, "socket", create)

    assert devices_listener._prebind()[1] == 47902
    assert devices_listener._prebind()[1] == 47902
    assert [sock.bound for sock in sockets] == [
        ("::", 47901),
        ("::", 47902),
        ("::", 47901),
        ("::", 47902),
    ]


def test_prebind_returns_error_after_every_approved_port_is_held(monkeypatch):
    create, sockets = _socket_factory([errno.EADDRINUSE] * 10)
    monkeypatch.setattr(devices_listener.socket, "socket", create)

    assert devices_listener._prebind() == (None, None, "No approved Devices port is available")
    assert [sock.bound[1] for sock in sockets] == list(range(47901, 47911))
    assert all(sock.closed for sock in sockets)


@pytest.mark.parametrize(
    ("bind_error", "expected_error"),
    [(errno.EACCES, os.strerror(errno.EACCES)), (OSError("socket setup failed"), "socket setup failed")],
)
def test_prebind_stops_on_other_socket_error_and_exposes_it(monkeypatch, bind_error, expected_error):
    create, sockets = _socket_factory([bind_error])
    monkeypatch.setattr(devices_listener.socket, "socket", create)

    assert devices_listener._prebind() == (None, None, expected_error)
    assert sockets[0].closed is True


@pytest.mark.asyncio
async def test_listener_start_surfaces_a_prebind_error_without_starting_a_server(monkeypatch):
    monkeypatch.setattr(devices_listener, "ensure_host_key", lambda: ("key", "cert"))
    monkeypatch.setattr(devices_listener, "_prebind", lambda: (None, None, "Permission denied"))
    listener = DevicesListener(app=object())

    assert await listener.start() is False
    assert listener.listening == {"error": "Permission denied"}
    assert listener.server is None
    assert listener.task is None
