"""Real Darwin integration coverage for Devices listener discovery.

This test deliberately uses no DNS-SD or socket seams: CI's macOS runtime must
prove the operating system accepts the fallback listener and processes the
native registration callback. It registers a unique instance name, because the
product's default (the computer's name, with no automatic rename) conflicts
with a CORSIA install already announcing on the same Mac.
"""

from __future__ import annotations

import asyncio
import functools
import os
import random
import socket
import subprocess
import uuid

import pytest

from infra import devices_identity, devices_listener
from infra.dns_sd import DNSServiceAnnouncement
from infra.devices_listener import DevicesListener


async def _empty_asgi_app(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


# Below the ephemeral range (49152-65535 on macOS), so between the probe closing
# and the listener binding, no outgoing connection or bind(0) elsewhere on the
# machine can be handed the fallback port; an OS-picked port (bind 0) sat inside
# that range and its neighbour could be taken in the gap.
_PAIR_PORTS = range(20000, 40000)


def _reserve_adjacent_port_pair() -> tuple[socket.socket, int, int]:
    """Hold a port outside the ephemeral range until the listener proves its fallback."""
    for _ in range(32):
        holder = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            for candidate in (holder, probe):
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                candidate.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            primary = random.choice(_PAIR_PORTS)
            holder.bind(("::", primary))
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


# GitHub's hosted macOS VM never lets mDNSResponder finish probing a name (30 s
# waited, en0/en1/anpi0/utun all present), so the callback is not provable there.
# The skip is decided before the run, never on a timeout, so a real Mac where the
# callback stops arriving still fails. Callback delivery is proved on a desktop run.
_HOSTED_RUNNER = os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"


async def _wait_for_registration(listener, timeout: float = 5) -> None:
    """Wait for DNSServiceProcessResult to invoke the real registration callback.

    The callback arrives once mDNSResponder has probed the name on an interface.
    A timeout fails the test and names the interfaces the machine offered.
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
    test_name = f"corsia-test-{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr(
        devices_listener, "DNSServiceAnnouncement",
        functools.partial(DNSServiceAnnouncement, name=test_name),
    )
    listener = DevicesListener(app=_empty_asgi_app)
    announcement = None

    try:
        assert await listener.start() is True, f"start refused: {listener.reason}: {listener.error}"
        assert listener.port == fallback
        announcement = listener.announcement
        assert announcement is not None
        key_path, cert_path = devices_identity._key_paths(key_root)
        assert key_path.exists()
        assert cert_path.exists()
        assert announcement.port == fallback
        assert announcement._ref is not None

        if not _HOSTED_RUNNER:
            await _wait_for_registration(listener)
            assert announcement.announced_name == test_name
        assert announcement.error is None
    finally:
        if listener.task is not None:
            await listener.stop()
        holder.close()

    assert announcement is not None
    assert announcement._ref is None
    assert announcement._fd is None
    assert announcement._callback is None
