from __future__ import annotations

import errno
import logging
import os
import socket
import stat
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from infra import devices_listener
from infra.devices_listener import (
    DevicesApp,
    DevicesListener,
    DevicesServer,
    ensure_host_key,
    lan_addresses,
    local_hostname,
    macos_computer_name,
    remote_connect_name,
)


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


def test_host_key_allows_read_only_private_files(tmp_path):
    key_path, cert_path = ensure_host_key(tmp_path)
    os.chmod(key_path, 0o400)
    os.chmod(cert_path, 0o400)

    assert ensure_host_key(tmp_path) == (key_path, cert_path)


def test_cert_only_recovery_regenerates_a_matching_host_identity(tmp_path):
    key_path, cert_path = ensure_host_key(tmp_path)
    previous_cert = cert_path.read_bytes()
    key_path.unlink()

    recovered_key, recovered_cert = ensure_host_key(tmp_path)
    key = serialization.load_pem_private_key(recovered_key.read_bytes(), password=None)
    cert = x509.load_pem_x509_certificate(recovered_cert.read_bytes())

    assert recovered_cert.read_bytes() != previous_cert
    assert key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    ) == cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_key_only_recovery_preserves_its_spki(tmp_path):
    key_path, cert_path = ensure_host_key(tmp_path)
    previous_spki = x509.load_pem_x509_certificate(cert_path.read_bytes()).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    cert_path.unlink()

    _, recovered_cert = ensure_host_key(tmp_path)
    recovered_spki = x509.load_pem_x509_certificate(recovered_cert.read_bytes()).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    assert key_path.exists()
    assert recovered_spki == previous_spki


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


def test_macos_computer_name_decodes_a_cfstring_as_utf8(monkeypatch):
    name = "Stúdió Mac"
    copied_name = object()
    released = []

    def copy_name(store, encoding):
        assert (store, encoding) == (None, None)
        return copied_name

    def string_length(value):
        assert value is copied_name
        return len(name)

    def max_size(length, encoding):
        assert (length, encoding) == (len(name), 0x08000100)
        return len(name.encode("utf-8"))

    def get_string(value, buffer, size, encoding):
        assert (value, size, encoding) == (copied_name, len(name.encode("utf-8")) + 1, 0x08000100)
        buffer.value = name.encode("utf-8")
        return True

    def release(value):
        released.append(value)

    system_configuration = SimpleNamespace(SCDynamicStoreCopyComputerName=copy_name)
    core_foundation = SimpleNamespace(
        CFStringGetLength=string_length,
        CFStringGetMaximumSizeForEncoding=max_size,
        CFStringGetCString=get_string,
        CFRelease=release,
    )
    monkeypatch.setattr(devices_listener.sys, "platform", "darwin")
    monkeypatch.setattr(
        devices_listener,
        "_load_macos_frameworks",
        lambda: (system_configuration, core_foundation),
    )

    assert macos_computer_name() == name
    assert released == [copied_name]


def test_remote_connect_name_uses_announced_name_before_macos_name(monkeypatch):
    monkeypatch.setattr(devices_listener, "macos_computer_name", lambda: "System name")
    monkeypatch.setattr(devices_listener, "local_hostname", lambda: "fallback.local")

    assert remote_connect_name(SimpleNamespace(announced_name="Announced name")) == "Announced name"
    assert remote_connect_name(SimpleNamespace(announced_name=None)) == "System name"


def test_remote_connect_name_falls_back_to_local_hostname(monkeypatch):
    monkeypatch.setattr(devices_listener, "macos_computer_name", lambda: None)
    monkeypatch.setattr(devices_listener, "local_hostname", lambda: "fallback.local")

    assert remote_connect_name(None) == "fallback.local"


class _FakeSocket:
    def __init__(self, bind_error: int | OSError | None = None):
        self.bind_error = bind_error
        self.options: list[tuple[int, int, int]] = []
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


@pytest.mark.asyncio
async def test_listener_start_contains_host_key_failure(monkeypatch, caplog):
    monkeypatch.setattr(
        devices_listener,
        "ensure_host_key",
        lambda: (_ for _ in ()).throw(RuntimeError("unsafe key")),
    )
    listener = DevicesListener(app=object())

    with caplog.at_level(logging.ERROR, logger="infra.devices_listener"):
        assert await listener.start() is False

    assert listener.listening == {"error": "Devices listener credentials unavailable"}
    assert listener.server is None
    assert listener.task is None
    assert "unsafe key" in caplog.text


@pytest.mark.asyncio
async def test_devices_app_authentication_uses_the_db_boundary(monkeypatch):
    calls = []

    async def fake_run_in_db(function, *args):
        calls.append((function, args))
        return {"token_kind": "session"}

    monkeypatch.setattr(devices_listener, "run_in_db", fake_run_in_db)

    assert await DevicesApp._has_human_session({"headers": [(b"authorization", b"Bearer token")]})
    assert calls == [(devices_listener.authenticate, ("token",))]


@pytest.mark.asyncio
async def test_devices_server_uses_uvicorn_default_wait_outside_049(monkeypatch):
    calls = []

    async def default_wait(self):
        calls.append(self)

    monkeypatch.setattr(devices_listener.uvicorn, "__version__", "0.50.0")
    monkeypatch.setattr(devices_listener.uvicorn.Server, "_wait_tasks_to_complete", default_wait)
    server = object.__new__(DevicesServer)

    await server._wait_tasks_to_complete()

    assert calls == [server]
