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


@pytest.mark.parametrize(('requested', 'reported', 'same'), [
    ('https://example.com', 'https://example.com/', True),
    ('https://Example.com/a?b=1', 'https://example.com/a?b=1', True),
    ('https://example.com/a', 'https://example.com/a/', False),
    ('https://example.com/?b=1', 'https://example.com/', False),
    ('https://example.com/', None, False),
])
def test_share_compares_addresses_as_chrome_reports_them(requested, reported, same):
    # A host-root share must not reload the page it is sharing (and lose a captcha or form in progress).
    assert browser._same_page_url(reported, requested) is same


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
    assert await tool.execute(tab='t3', list_tabs=True, _thread_id='thread') == 'ok'
    assert calls == [
        (['tab'], 'thread'), (['tab', 't2'], 'thread'), (['snapshot'], 'thread'), (['tab', 't9'], 'thread'),
        (['tab', 't3'], 'thread'), (['tab'], 'thread'),
    ]
    assert browser_session_commands._saved_command(('tab', 't2'), pane=False)
    assert not browser_session_commands._saved_command(('tab', 'new'), pane=False)
    assert not browser_session_commands._saved_command(('tab', 'close'), pane=False)


async def _watch(monkeypatch, replay, pages):
    """Run one target-watch connection over `replay` and return how many notices it sent."""
    events = asyncio.Queue()
    for event in replay:
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

        async def send(self, method, **params):
            assert method == 'Target.setDiscoverTargets' and params == {'discover': True}
            return 1

    async def endpoint(*_args):
        return 'ws://browser.test'

    monkeypatch.setattr(browser_session_commands, 'browser_endpoint', endpoint)
    monkeypatch.setattr(browser_cdp, 'CdpClient', Client)
    task = asyncio.create_task(browser_pane.watch_targets('thread', lambda: notified.append(True), pages))
    for _ in range(20):
        if events.empty():
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return len(notified)


def _page(method, target):
    if method == 'Target.targetDestroyed':
        return {'method': method, 'params': {'targetId': target}}
    return {'method': method, 'params': {'targetInfo': {'type': 'page', 'targetId': target}}}


@pytest.mark.asyncio
async def test_target_discovery_notifies_only_on_page_create_and_destroy(monkeypatch):
    assert await _watch(monkeypatch, [
        # Discovery replays the tab already open before it answers; that replay is no change.
        {'method': 'Target.targetCreated', 'params': {'targetInfo': {'type': 'page', 'targetId': 'first'}}},
        {'id': 1, 'result': {}},
        # A captcha's frames and workers come and go without touching the tabs.
        {'method': 'Target.targetCreated', 'params': {'targetInfo': {'type': 'iframe', 'targetId': 'frame'}}},
        {'method': 'Target.targetDestroyed', 'params': {'targetId': 'frame'}},
        {'method': 'Target.targetCreated', 'params': {'targetInfo': {'type': 'worker', 'targetId': 'worker'}}},
        {'method': 'Target.targetDestroyed', 'params': {'targetId': 'worker'}},
        {'method': 'Target.targetCreated', 'params': {'targetInfo': {'type': 'page', 'targetId': 'popup'}}},
        {'method': 'Target.targetDestroyed', 'params': {'targetId': 'popup'}},
        {'method': 'Target.targetDestroyed', 'params': {'targetId': 'first'}},
    ], [None]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(('replayed', 'notices'), [
    # The same tabs come back: nothing changed while the watch was down.
    (['first', 'second'], 0),
    # A tab opened or closed during the gap: one notice when discovery answers.
    (['first', 'second', 'third'], 1),
    (['first'], 1),
])
async def test_reconnected_target_watch_notifies_once_for_a_missed_tab_change(monkeypatch, replayed, notices):
    pages = [None]
    assert await _watch(monkeypatch, [_page('Target.targetCreated', 'first'), _page('Target.targetCreated', 'second'),
                                      {'id': 1, 'result': {}}], pages) == 0
    assert pages[0] == {'first', 'second'}
    replay = [_page('Target.targetCreated', target) for target in replayed] + [{'id': 1, 'result': {}}]
    assert await _watch(monkeypatch, replay, pages) == notices
    assert pages[0] == set(replayed)


@pytest.mark.asyncio
async def test_reconnected_target_watch_notifies_when_empty_set_gains_a_tab(monkeypatch):
    pages = [None]
    assert await _watch(monkeypatch, [{'id': 1, 'result': {}}], pages) == 0
    assert pages[0] == set()
    assert await _watch(monkeypatch, [_page('Target.targetCreated', 'first'), {'id': 1, 'result': {}}], pages) == 1
    assert pages[0] == {'first'}


@pytest.mark.asyncio
async def test_target_change_restarts_page_streams_and_clears_endpoint(monkeypatch):
    started = []
    cleared = []
    events = []
    change_ready = asyncio.Event()

    async def stream(*_args):
        started.append(True)
        if len(started) >= 4:
            change_ready.set()
        await asyncio.Future()

    async def watch(_thread, _notify, _pages):
        await asyncio.Future()

    monkeypatch.setattr(browser_stream, 'emit_product_event', lambda *args, **kwargs: events.append((args, kwargs)))
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
    assert events == [(('browser_pane', 'target_changed'), {'thread_id': 'thread'})]
    assert 'thread' not in browser_pane_state._target_changes


@pytest.mark.asyncio
async def test_lost_target_watch_keeps_the_pane_on_its_current_tab(monkeypatch):
    from host_access.browser_session_state import BrowserSessionError
    started = []
    restarted = asyncio.Event()
    watched = []

    async def stream(*_args):
        started.append(True)
        if len(started) >= 4:
            restarted.set()
        await asyncio.Future()

    async def watch(_thread, _notify, _pages):
        watched.append(True)
        raise BrowserSessionError('target watch down')

    monkeypatch.setattr(browser_stream, 'PANE_RETRY_DELAYS', ())
    monkeypatch.setattr(browser_stream, 'emit_product_event', lambda *args, **kwargs: None)
    monkeypatch.setattr(browser_stream, '_screencast_events', stream)
    monkeypatch.setattr(browser_stream, '_metadata_events', stream)
    monkeypatch.setattr(browser_pane, 'watch_targets', watch)
    monkeypatch.setattr(browser_pane, 'clear_page_endpoint', lambda *args: None)
    task = asyncio.create_task(browser_stream._follow_page('thread', 'pane', object(), lambda _m: None))
    for _ in range(20):
        await asyncio.sleep(0)
    # The watch gave up, yet the pane still streams its tab without a restart or an error.
    assert watched == [True] and len(started) == 2 and not task.done()
    # An agent's own tab switch still moves the pane.
    browser_pane_state.target_changed('thread')
    await asyncio.wait_for(restarted.wait(), 1)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_queued_pointer_moves_collapse_as_they_arrive():
    move = lambda x: {'type': 'input_mouse', 'eventType': 'mouseMoved', 'x': x}  # noqa: E731
    click = {'type': 'input_mouse', 'eventType': 'mousePressed', 'x': 3}
    sent = asyncio.Queue()
    for message in (move(1), move(2), move(3), click, move(4)):
        sent.put_nowait(json.dumps(message))

    class Pane:
        async def receive_text(self):
            return await sent.get()

    messages = browser_stream._pane_messages(Pane())
    # A stalled page holds at most one waiting move: moves behind a move replace it, a click never does.
    assert [await anext(messages) for _ in range(3)] == [move(3), click, move(4)]
    await messages.aclose()
