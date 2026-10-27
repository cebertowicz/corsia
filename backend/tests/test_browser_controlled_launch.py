"""Owned browser listeners, launch defaults and process cleanup."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from host_access import browser_chrome, browser_cli, browser_launch, browser_ports, browser_session_launch
from host_access.browser_session_state import BrowserRuntime, BrowserSessionRecord
from tools.host_path_policy import ProtectedHostPathPolicy
from tests.browser_process_fixture import synthetic_browser  # noqa: F401


def test_ports_are_distinct_reserved_and_free_checked(monkeypatch):
    occupied = socket.socket()
    occupied.bind(('127.0.0.1', 0))
    busy = occupied.getsockname()[1]
    available = socket.socket()
    available.bind(('127.0.0.1', 0))
    free = available.getsockname()[1]
    available.close()
    monkeypatch.setattr(browser_ports, 'RESERVED_PORTS', (busy, free))
    try:
        with pytest.raises(RuntimeError, match='listener ports are unavailable'):
            browser_ports.reserve_pair()
        # A partial allocation is released when the second port is unavailable.
        probe = socket.socket()
        try:
            probe.bind(('127.0.0.1', free))
        finally:
            probe.close()
    finally:
        occupied.close()


def test_reserved_inventory_contains_only_chrome_restricted_ports():
    # Independent copy of kRestrictedPorts, Chromium 154.0.8037.58 net/base/port_util.cc.
    restricted = {
        0, 1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53,
        69, 77, 79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117,
        119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514,
        515, 526, 530, 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989,
        990, 993, 995, 1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000,
        6566, 6665, 6666, 6667, 6668, 6669, 6697, 10080,
    }
    assert isinstance(browser_ports.RESERVED_PORTS, tuple)
    assert len(set(browser_ports.RESERVED_PORTS)) == len(browser_ports.RESERVED_PORTS)
    assert set(browser_ports.RESERVED_PORTS) <= restricted
    assert all(port >= 1024 for port in browser_ports.RESERVED_PORTS)
    assert len(browser_ports.RESERVED_PORTS) // 2 >= 8


def test_reservation_skips_busy_ports(monkeypatch):
    occupied = socket.socket()
    occupied.bind(('127.0.0.1', 0))
    try:
        monkeypatch.setattr(browser_ports, 'RESERVED_PORTS', (occupied.getsockname()[1], 0, 0))
        first, second = browser_ports.reserve_pair()
        try:
            assert len({occupied.getsockname()[1], first.getsockname()[1], second.getsockname()[1]}) == 3
        finally:
            first.close()
            second.close()
    finally:
        occupied.close()


def test_normal_launch_flags_and_stealth_have_one_owner(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_BROWSER_ARGS', '--no-sandbox')
    argv = browser_chrome.chrome_argv('chrome', 39400, tmp_path, headless=True, ua='Fixture UA')
    assert argv[0] == 'chrome'
    assert '--remote-debugging-port=39400' in argv
    assert '--remote-debugging-port=0' not in argv
    assert not any(arg.startswith('--remote-allow-origins') for arg in argv)
    assert not any(arg.startswith('--explicitly-allowed-ports') for arg in argv)
    assert '--use-mock-keychain' in argv and '--password-store=basic' in argv
    assert '--disable-background-networking' in argv and '--no-first-run' in argv
    assert f'--user-data-dir={tmp_path}' in argv
    assert '--headless=new' in argv and '--no-sandbox' in argv
    assert '--user-agent=Fixture UA' in argv
    assert '--disable-blink-features=AutomationControlled' in argv


@pytest.mark.parametrize('override', [
    '--explicitly-allowed-ports=1719,1720', '-explicitly-allowed-ports=10080',
    '--explicitly-allowed-ports\n1719', '--remote-allow-origins=*',
    '-remote-allow-origins=http://localhost', '--remote-allow-origins *',
    '--remote-debugging-port=9222', '-remote-debugging-port=0',
    '--remote-debugging-port\n9222', '--remote-debugging-port 9222',
    '--remote-debugging-address=0.0.0.0', '-remote-debugging-address=::',
    '--remote-debugging-address\n0.0.0.0', '--remote-debugging-address 0.0.0.0',
    '--user-data-dir=/fixture/profile', '-user-data-dir=/fixture/profile',
    '--user-data-dir\n/fixture/profile', '--user-data-dir /fixture/profile',
    '--no-sandbox\r--remote-debugging-port=9222',
    '--no-sandbox\v--user-data-dir=/fixture/profile',
    '--no-sandbox\f--remote-debugging-address=::',
    '--no-sandbox\u0085--explicitly-allowed-ports=10080',
    '--no-sandbox\u2028--remote-allow-origins=*',
])
async def test_protection_overrides_are_refused_before_spawn(tmp_path, monkeypatch, override):
    from host_access.browser_identity import BrowserIdentityError
    monkeypatch.setenv('AGENT_BROWSER_ARGS', '--no-sandbox\n' + override)
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path)
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    # Public admission rejects it even before pinning the binaries or creating a session.
    with pytest.raises(BrowserIdentityError, match='remove it from AGENT_BROWSER_ARGS'):
        browser_cli.check_local_mode(('open', 'about:blank'))
    # The actual producer also refuses it if an internal caller bypasses admission.
    with pytest.raises(BrowserIdentityError, match='remove it from AGENT_BROWSER_ARGS'):
        await browser_chrome.ChromeProcess.launch('fixture', profile=None, headless=True, ua=None)
    spawn.assert_not_awaited()
    assert not list(tmp_path.iterdir())


def test_hard_floor_includes_static_network_and_socket_rules(tmp_path):
    policy = ProtectedHostPathPolicy(home=tmp_path / 'home', state_root=tmp_path / 'state')
    floor = policy.hard_floor_sandbox_rules()
    for port in browser_ports.RESERVED_PORTS:
        assert f'(deny network-outbound (remote tcp "*:{port}"))' in floor
        assert f'(deny network-bind (local tcp "*:{port}"))' in floor
    assert str(browser_ports.SOCKET_ROOT) in floor
    assert 'remote unix-socket' in floor


@pytest.mark.parametrize('reported', [None, 0, 45000])
async def test_stream_fallback_or_missing_status_is_refused(reported):
    runtime = BrowserRuntime(BrowserSessionRecord('thread', 'session'))
    runtime.chrome = SimpleNamespace(stream_port=39401)
    runtime.record = replace(runtime.record, binding=SimpleNamespace(pid=os.getpid()))
    owner = SimpleNamespace(_session_argv=lambda *_: (), _invoke=AsyncMock(return_value=
        browser_cli.CommandResult(0, json.dumps({'data': {'port': reported}}), '')))
    with pytest.raises(RuntimeError, match='reserved listener'):
        await browser_session_launch.verify_stream(owner, runtime)


async def test_close_reaps_owned_group_and_removes_only_temporary_profile(tmp_path):
    profile = tmp_path / 'temporary'
    profile.mkdir()
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)',
                                                  start_new_session=True)
    owned = browser_chrome.ChromeProcess(process, 39400, 39401, None, profile)
    try:
        await owned.close()
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
        assert not profile.exists()
        assert owned.closed
        await owned.close()  # The owning handle remains retry-safe after success.
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_linux_saved_profile_falls_back_with_agent_notice(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    monkeypatch.setattr(browser_session_launch.sys, 'platform', 'linux')
    monkeypatch.setattr(browser_launch, '_owner_key', lambda _: 'fixture-owner')
    rendered = await fake.owner.run('linux', ['open', 'about:blank'])
    record = fake.owner._sessions['linux'].record
    assert record.profile is None and record.copy_of is None
    assert 'Saved browser sign-ins are unavailable on Linux yet; this session is ephemeral.' in rendered
    assert not fake.chrome_calls  # Linux keeps dev's native-owned ephemeral launch.
    assert all('--cdp' not in call and '--profile' not in call for call in fake.calls)
    assert 'this session is ephemeral' not in await fake.owner.run('linux', ['snapshot'])
    from host_access.processes import ProcessOwner
    from tools import browser_runtime
    from tools.browser import BrowserOpenTool
    process_owner = ProcessOwner.__new__(ProcessOwner)
    process_owner._browser_owner = fake.owner
    monkeypatch.setattr(browser_runtime, 'PROCESS_OWNER', process_owner)
    monkeypatch.setattr(browser_runtime, '_check_installed', lambda: None)
    opened = await BrowserOpenTool().execute('https://example.com', wait_for_load=False, _thread_id='native-linux')
    assert 'this session is ephemeral' in opened
    await fake.owner.shutdown()


@pytest.mark.parametrize('saved', [False, True])
async def test_stream_mismatch_closes_daemon_and_chrome_before_reuse(synthetic_browser, monkeypatch, saved):
    from host_access.browser_session_state import BrowserSessionError
    fake = synthetic_browser
    monkeypatch.setattr(browser_launch, '_owner_key', lambda _: 'owner' if saved else None)
    handles = []
    original = browser_session_launch.start_chrome

    async def start(owner, runtime):
        try:
            await original(owner, runtime)
        finally:
            if runtime.chrome is not None:
                handles.append(runtime.chrome)

    monkeypatch.setattr(browser_session_launch, 'start_chrome', start)
    monkeypatch.setattr(browser_session_launch, 'verify_stream', AsyncMock(side_effect=RuntimeError('stream mismatch')))
    with pytest.raises(BrowserSessionError, match='startup failed'):
        await fake.owner.run('thread', ['open', 'about:blank'])
    assert fake.owner.live_count() == 0
    assert handles and handles[0].close.await_count == 1
    assert all(child.poll() is not None for child in fake.all_children)


async def test_cancellation_during_spawn_reaps_the_eventual_owned_child(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path)
    actual_spawn = asyncio.create_subprocess_exec
    created, release = asyncio.Event(), asyncio.Event()
    children = []

    async def delayed_spawn(*args, **kwargs):
        child = await actual_spawn(sys.executable, '-c', 'import time; time.sleep(60)', start_new_session=True)
        children.append(child)
        created.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_spawn)
    task = asyncio.create_task(browser_chrome.ChromeProcess.launch('fixture', profile=None, headless=True, ua=None))
    try:
        await asyncio.wait_for(created.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None
        assert not list(tmp_path.iterdir())
    finally:
        release.set()
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()


def test_a_foreign_listener_is_not_accepted(monkeypatch):
    listener = SimpleNamespace(status='LISTEN', laddr=SimpleNamespace(ip='127.0.0.1', port=39401))
    monkeypatch.setattr(browser_chrome.psutil, 'Process', lambda _: SimpleNamespace(net_connections=lambda **_: [listener]))
    with pytest.raises(RuntimeError, match='ownership'):
        browser_chrome.require_listener(123, 39400)


async def test_failed_chrome_start_releases_ports_and_temporary_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_launch, 'profile_root', lambda: tmp_path)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(side_effect=OSError('fixture spawn failure')))
    before = set(tmp_path.iterdir())
    with pytest.raises(OSError, match='fixture spawn failure'):
        await browser_chrome.ChromeProcess.launch('missing-chrome', profile=None, headless=True, ua=None)
    assert set(tmp_path.iterdir()) == before
    first, second = browser_ports.reserve_pair()
    try:
        assert first.getsockname()[1] != second.getsockname()[1]
    finally:
        first.close()
        second.close()
