"""Unit coverage for the native test's best-effort clipboard trace."""
import asyncio
from types import SimpleNamespace

import pytest

from tests import browser_clipboard_diagnostics as diagnostics


@pytest.mark.asyncio
async def test_failed_first_drag_reports_pointer_state_before_any_clipboard_call(monkeypatch, capsys):
    calls = []

    class Owner:
        async def run(self, *args, **kwargs):
            pass

        async def page_endpoint(self, thread):
            return 'owned-page'

    class Client:
        def __init__(self, endpoint):
            self.endpoint = endpoint

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call(self, method, **params):
            calls.append(params['expression'])
            return {'result': {'value': {'drags': [{'events': []}]}}}

    async def mouse(thread, event, connection):
        calls.append('mouse')
        return None

    async def drag(thread, connection, start, end):
        await module.browser_pane.mouse_event(thread, {'eventType': 'mousePressed'}, connection)
        raise AssertionError('selection failed')

    monkeypatch.setattr(diagnostics.clipboard, 'CdpClient', Client)
    module = SimpleNamespace(_drag_selection=drag, browser_pane=SimpleNamespace(mouse_event=mouse))
    request = SimpleNamespace(module=module, session=SimpleNamespace(testsfailed=0))
    fixture = diagnostics.native_clipboard_diagnostics.__wrapped__(Owner(), monkeypatch, request)
    await fixture.__anext__()
    with pytest.raises(AssertionError):
        await module._drag_selection('thread', 'pane', [1, 2], [3, 2])
    request.session.testsfailed = 1
    with pytest.raises(StopAsyncIteration):
        await fixture.__anext__()
    assert '"start": [1, 2], "end": [3, 2]' in calls[0]
    assert calls[1] == 'mouse'
    assert calls[2] == diagnostics._POINTER_RAF
    assert calls[3] == diagnostics._POINTER_REPORT
    output = capsys.readouterr().out
    assert '"pointer"' in output
    assert '"eventType": "mousePressed"' in output
    assert '"result": "None"' in output


@pytest.mark.asyncio
async def test_pre_key_probe_failure_does_not_block_clipboard_dispatch(monkeypatch):
    trace = diagnostics._ClipboardTrace()
    calls = []

    class Client:
        endpoint = 'test-endpoint'

        async def call(self, method, **params):
            calls.append(method)
            raise RuntimeError('probe unavailable')

    async def dispatch(cdp, method, step, **params):
        calls.append(step)
        return {'ok': True}

    async def no_display(result):
        result['unavailable'] = 'test'

    trace.original_call = dispatch
    monkeypatch.setattr(diagnostics, '_x_focus', no_display)
    result = await trace.call(Client(), 'Input.dispatchKeyEvent', 'copy key down', type='keyDown')

    assert result == {'ok': True}
    assert calls == ['Target.getTargetInfo', 'Page.getFrameTree', 'Runtime.evaluate', 'copy key down']
    before = trace.events[0]['before_key']
    assert before['focused_frame_id'] is None
    assert 'diagnostic_error' in before['page']


@pytest.mark.asyncio
async def test_child_probe_is_taken_before_dispatch(monkeypatch):
    calls = []

    class Client:
        endpoint = 'test-endpoint'

        async def call(self, method, **params):
            calls.append((method, params.get('expression')))
            if params.get('expression') == diagnostics._FOCUS_STATE:
                return {'result': {'value': {'inaccessibleFrame': True}}}
            if params.get('expression') == diagnostics._CHILD_FOCUS_STATE:
                return {'result': {'value': {'selected': 'Cross origin', 'hasFocus': True}}}
            return {}

    async def no_display(result):
        result['unavailable'] = 'test'

    monkeypatch.setattr(diagnostics, '_x_focus', no_display)
    trace = diagnostics._ClipboardTrace()

    async def dispatch(cdp, method, step, **params):
        calls.append(('dispatch', None))
        return {}

    trace.original_call = dispatch
    await trace.call(Client(), 'Input.dispatchKeyEvent', 'copy key down', type='keyDown')
    assert calls[-2:] == [('Runtime.evaluate', diagnostics._CHILD_FOCUS_STATE), ('dispatch', None)]
    assert trace.events[0]['before_key']['child_at_pre_key']['result']['value']['selected'] == 'Cross origin'


@pytest.mark.asyncio
async def test_x_command_kills_process_when_cancelled(monkeypatch):
    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waited = True
            self.returncode = -9

    process = Process()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    task = asyncio.create_task(diagnostics._x_command('xwininfo', '-root', '-tree'))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.waited


@pytest.mark.asyncio
async def test_x_focus_reports_child_map_when_xdotool_missing(monkeypatch):
    monkeypatch.setenv('DISPLAY', ':test')
    monkeypatch.setattr(diagnostics.shutil, 'which', lambda name: None if name == 'xdotool' else name)

    async def command(executable, *args, environment=None):
        if args == ('-root', '-tree'):
            return {'exit': 0, 'output': 'root id: 0x1\n  0xabc "Browser": ()\n'}
        if args == ('-id', '0xabc'):
            return {'exit': 0, 'output': '  Map State: IsViewable'}
        return {'exit': 0, 'output': '_NET_ACTIVE_WINDOW: not found'}

    monkeypatch.setattr(diagnostics, '_x_command', command)
    result = {}
    await diagnostics._x_focus(result)
    assert result['input_focus'] == 'unavailable: tool not installed'
    assert result['window_map'] == [{'window': '0xabc', 'map_state': ['Map State: IsViewable'], 'exit': 0}]


def test_x_environment_uses_only_the_fixture_profile(monkeypatch):
    monkeypatch.delenv('DISPLAY', raising=False)
    monkeypatch.setenv('AGENT_BROWSER_PROFILE', '/test/own-profile')

    class Process:
        def __init__(self, profile):
            self.info = {'cmdline': [f'--user-data-dir={profile}']}

        def environ(self):
            assert self.info['cmdline'] == ['--user-data-dir=/test/own-profile']
            return {'DISPLAY': ':owned', 'XAUTHORITY': '/test/own-xauth'}

    monkeypatch.setattr(diagnostics.psutil, 'process_iter', lambda _: [
        Process('/test/unrelated-profile'), Process('/test/own-profile')])
    environment = diagnostics._x_environment()
    assert environment['DISPLAY'] == ':owned'
    assert environment['XAUTHORITY'] == '/test/own-xauth'
