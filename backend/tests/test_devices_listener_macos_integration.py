"""Real Darwin integration coverage for Devices listener discovery.

This test deliberately uses no DNS-SD or socket seams: CI's macOS runtime must
prove the operating system accepts the fallback listener and processes the
native registration callback.
"""

from __future__ import annotations

import asyncio
import socket

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


async def _wait_for_registration(listener, timeout: float = 5) -> None:
    """Wait for DNSServiceProcessResult to invoke the real registration callback."""
    announcement = listener.announcement
    assert announcement is not None
    deadline = asyncio.get_running_loop().time() + timeout
    while announcement.announced_name is None and announcement.error is None:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("DNS-SD registration callback did not arrive within five seconds")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_real_dns_sd_announces_and_deallocates_the_actual_fallback_port(monkeypatch):
    """Hold the preferred port, then exercise real bind, DNSService, and teardown APIs."""
    holder, primary, fallback = _reserve_adjacent_port_pair()
    monkeypatch.setattr(devices_listener, "DEFAULT_PORT", primary)
    monkeypatch.setattr(devices_listener, "FALLBACK_PORTS", (fallback,))
    listener = DevicesListener(app=_empty_asgi_app)
    announcement = None

    try:
        assert await listener.start() is True
        assert listener.port == fallback
        announcement = listener.announcement
        assert announcement is not None
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
