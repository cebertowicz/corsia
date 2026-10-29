"""Readiness waits for the owned Chrome window and a frame in one budget."""
import asyncio

import pytest

from tests import browser_native_window as window


@pytest.mark.asyncio
async def test_wait_retries_discovery_and_uses_actual_profile(monkeypatch):
    discoveries = iter([None, ({'DISPLAY': ':owned'}, '/tmp/actual-profile')])
    commands = []
    seen_sockets = []

    def discover(socket_dir):
        seen_sockets.append(socket_dir)
        return next(discoveries)

    monkeypatch.setattr(window, '_x_environment', discover)
    monkeypatch.setattr(window.shutil, 'which', lambda name: name)

    async def command(executable, *args, environment):
        commands.append((args, environment))
        if args == ('-root', '-tree'):
            return {'exit': 0, 'output': '  0xabc "Chrome /tmp/actual-profile"\n'}
        return {'exit': 0, 'output': 'Map State: IsViewable'}

    class Cast:
        def __init__(self, endpoint, send):
            self.send = send

        async def run(self):
            self.send('{"data":"frame"}')
            await asyncio.Event().wait()

    monkeypatch.setattr(window, '_x_command', command)
    monkeypatch.setattr(window.browser_screencast, 'PaneScreencast', Cast)
    await window.wait_for_headed_window('endpoint', '/owned/sockets')
    assert seen_sockets == ['/owned/sockets', '/owned/sockets']
    assert commands == [(('-root', '-tree'), {'DISPLAY': ':owned'}),
                        (('-id', '0xabc'), {'DISPLAY': ':owned'})]


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['discovery', 'map', 'frame'])
async def test_one_budget_covers_each_readiness_phase(monkeypatch, phase):
    real_timeout = asyncio.timeout
    budgets = []

    def short_timeout(seconds):
        budgets.append(seconds)
        return real_timeout(0.01)

    monkeypatch.setattr(window.asyncio, 'timeout', short_timeout)
    monkeypatch.setattr(window.shutil, 'which', lambda name: name)
    monkeypatch.setattr(window, '_x_environment', lambda socket_dir: None if phase == 'discovery'
                        else ({'DISPLAY': ':owned'}, '/actual'))

    async def command(executable, *args, environment):
        if phase == 'map':
            await asyncio.Event().wait()
        if args == ('-root', '-tree'):
            return {'exit': 0, 'output': '  0xabc "Chrome /actual"\n'}
        return {'exit': 0, 'output': 'Map State: IsViewable'}

    class Cast:
        def __init__(self, endpoint, send):
            pass

        async def run(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(window, '_x_command', command)
    monkeypatch.setattr(window.browser_screencast, 'PaneScreencast', Cast)
    message = 'no owned X display' if phase == 'discovery' else 'window or frame did not become ready'
    with pytest.raises(AssertionError, match=message):
        await window.wait_for_headed_window('endpoint', '/owned/sockets')
    assert budgets == [10]
