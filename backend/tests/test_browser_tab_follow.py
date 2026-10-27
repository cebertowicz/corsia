import asyncio
import json

import pytest

from host_access import browser_cdp, browser_pane, browser_pane_state, browser_session_commands
from tools import browser, browser_actions
from ws import browser_stream


@pytest.mark.asyncio
@pytest.mark.parametrize(('show_window', 'shared', 'navigates'), [
    (True, False, False),
    (True, True, True),
    (False, False, True),
    (False, True, True),
])
async def test_same_url_only_skips_navigation_for_first_share(monkeypatch, show_window, shared, navigates):
    from host_access import processes

    class Sessions:
        async def has_session(self, _thread):
            return True

        async def is_shared(self, _thread):
            return shared

    class Owner:
        def browser_sessions(self):
            return Sessions()

    calls = []

    async def run(args, **kwargs):
        calls.append((args, kwargs))
        return '{"url":"https://example.com/"}' if args[0] == 'get' else 'navigating'

    monkeypatch.setattr(processes, 'PROCESS_OWNER', Owner())
    monkeypatch.setattr(browser, '_run', run)
    result = await browser.BrowserOpenTool().execute(
        'https://example.com/', show_window=show_window, wait_for_load=False, _thread_id='thread')
    assert calls[0][0] == ['get', 'url']
    assert (len(calls) == 2) is navigates
    if navigates:
        assert calls[1][0][0] == 'eval'
        assert result == 'navigating'
    else:
        assert result == "Shared the current page in the person's CORSIA pane: https://example.com/"


@pytest.mark.asyncio
async def test_snapshot_lists_and_switches_only_existing_tab_refs(monkeypatch):
    calls = []

    async def run(args, **kwargs):
        calls.append((args, kwargs['thread_id']))
        return 'Error: no tab t9' if args == ['tab', 't9'] else 'ok'

    monkeypatch.setattr(browser_actions, '_run', run)
    tool = browser_actions.BrowserSnapshotTool()
    assert await tool.execute(list_tabs=True, _thread_id='thread') == 'ok'
    assert await tool.execute(tab='t2', _thread_id='thread') == 'ok'
    assert await tool.execute(tab='t9', _thread_id='thread') == 'Error: no tab t9'
    assert (await tool.execute(tab='new', _thread_id='thread')).startswith('Error:')
    assert (await tool.execute(tab='close', _thread_id='thread')).startswith('Error:')
    assert calls == [
        (['tab'], 'thread'), (['tab', 't2'], 'thread'), (['snapshot'], 'thread'), (['tab', 't9'], 'thread'),
    ]
    assert browser_session_commands._saved_command(('tab', 't2'), pane=False)
    assert not browser_session_commands._saved_command(('tab', 'new'), pane=False)
    assert not browser_session_commands._saved_command(('tab', 'close'), pane=False)


@pytest.mark.asyncio
async def test_target_discovery_notifies_on_page_create_and_destroy(monkeypatch):
    events = asyncio.Queue()
    for event in [
        {'method': 'Target.targetCreated', 'params': {'targetInfo': {'type': 'worker'}}},
        {'method': 'Target.targetCreated', 'params': {'targetInfo': {'type': 'page'}}},
        {'method': 'Target.targetDestroyed', 'params': {'targetId': 'page'}},
    ]:
        events.put_nowait(json.dumps(event))
    notified = []

    class Socket:
        async def recv(self):
            return await events.get()

    class Client:
        def __init__(self, endpoint):
            self.socket = Socket()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def call(self, method, **params):
            assert method == 'Target.setDiscoverTargets' and params == {'discover': True}
            return {}

    async def endpoint(*_args):
        return 'ws://browser.test'

    monkeypatch.setattr(browser_session_commands, 'browser_endpoint', endpoint)
    monkeypatch.setattr(browser_cdp, 'CdpClient', Client)
    task = asyncio.create_task(browser_pane.watch_targets('thread', lambda: notified.append(True)))
    for _ in range(10):
        if len(notified) == 2:
            break
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(notified) == 2


@pytest.mark.asyncio
async def test_target_change_restarts_page_streams_and_clears_endpoint(monkeypatch):
    started = []
    cleared = []
    change_ready = asyncio.Event()

    async def stream(*_args):
        started.append(True)
        if len(started) >= 4:
            change_ready.set()
        await asyncio.Future()

    async def watch(_thread, _notify):
        await asyncio.Future()

    monkeypatch.setattr(browser_stream, '_screencast_events', stream)
    monkeypatch.setattr(browser_stream, '_metadata_events', stream)
    monkeypatch.setattr(browser_pane, 'watch_targets', watch)
    monkeypatch.setattr(browser_pane, 'clear_page_endpoint', lambda *args: cleared.append(args))
    task = asyncio.create_task(browser_stream._follow_page('thread', 'pane', object(), lambda _m: None))
    for _ in range(20):
        if len(started) == 2:
            break
        await asyncio.sleep(0)
    browser_pane_state.target_changed('thread')
    await asyncio.wait_for(change_ready.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert cleared == [('thread', 'pane')]
    assert 'thread' not in browser_pane_state._target_changes
