"""Real Darwin integration coverage for Devices listener discovery.

This test deliberately uses no DNS-SD or socket seams: CI's macOS runtime must
prove the operating system accepts the fallback listener and processes the
native registration callback.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess

import pytest

from infra import devices_listener
from infra.devices_listener import DevicesListener


async def _empty_asgi_app(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def _reserve_adjacent_port_pair() -> tuple[socket.socket, int, int]:
    """Hold an OS-selected port until the listener proves its fallback."""
    for _ in range(32):
        holder = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            for candidate in (holder, probe):
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                candidate.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            holder.bind(("::", 0))
            primary = holder.getsockname()[1]
            if primary == 65535:
                holder.close()
                probe.close()
                continue
            probe.bind(("::", primary + 1))
            probe.close()
            return holder, primary, primary + 1
        except OSError:
            holder.close()
            probe.close()
    raise AssertionError("could not reserve an adjacent disposable TCP port pair")


def _interface_summary() -> str:
    """One line per network interface, so a timeout names what the runner offered."""
    result = subprocess.run(["ifconfig", "-a"], capture_output=True, text=True, timeout=10)
    return "; ".join(line for line in result.stdout.splitlines() if line and not line[0].isspace())


async def _wait_for_registration(listener, timeout: float = 30) -> None:
    """Wait for DNSServiceProcessResult to invoke the real registration callback.

    The callback arrives once mDNSResponder has probed the name on an interface.
    A hosted CI runner did not deliver it within five seconds although a
    desktop does in about one, so the bound is generous: the test proves the OS
    delivers the callback, not how fast, and a timeout names the interfaces.
    """
    announcement = listener.announcement
    assert announcement is not None
    started = asyncio.get_running_loop().time()
    while announcement.announced_name is None and announcement.error is None:
        elapsed = asyncio.get_running_loop().time() - started
        if elapsed >= timeout:
            raise AssertionError(
                f"DNS-SD registration callback did not arrive within {timeout:.0f} seconds; "
                f"interfaces: {_interface_summary()}"
            )
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_real_dns_sd_announces_and_deallocates_the_actual_fallback_port(monkeypatch, tmp_path):
    """Hold the preferred port, then exercise real bind, DNSService, and teardown APIs."""
    holder, primary, fallback = _reserve_adjacent_port_pair()
    key_root = tmp_path / "devices-state"
    ensure_host_key = devices_listener.ensure_host_key

    def ensure_test_host_key():
        return ensure_host_key(key_root)

    monkeypatch.setattr(devices_listener, "DEFAULT_PORT", primary)
    monkeypatch.setattr(devices_listener, "FALLBACK_PORTS", (fallback,))
    monkeypatch.setattr(devices_listener, "ensure_host_key", ensure_test_host_key)
    listener = DevicesListener(app=_empty_asgi_app)
    announcement = None

    try:
        assert await listener.start() is True
        assert listener.port == fallback
        announcement = listener.announcement
        assert announcement is not None
        key_path, cert_path = devices_listener._key_paths(key_root)
        assert key_path.exists()
        assert cert_path.exists()
        assert announcement.port == fallback
        assert announcement._ref is not None

        await _wait_for_registration(listener)
        assert announcement.error is None
        assert announcement.announced_name
    finally:
        if listener.task is not None:
            await listener.stop()
        holder.close()

    assert announcement is not None
    assert announcement._ref is None
    assert announcement._fd is None
    assert announcement._callback is None
