import asyncio
import json
import logging

import pytest

from host_access import browser_text_fields
from ws.browser_touch_fields import TouchFields


@pytest.mark.asyncio
async def test_only_field_geometry_and_kind_reaches_touch_pane(monkeypatch):
    secret = 'fixture-private-value'
    calls = []

    class FakeCdp:
        def __init__(self, _endpoint):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def call(self, method, **params):
            calls.append(method)
            if method == 'DOM.getDocument':
                return {'root': {'nodeId': 1, 'attributes': ['value', secret, 'name', 'private-name', 'id', 'private-id', 'placeholder', 'private-placeholder', 'aria-label', 'private-label']}}
            if method == 'DOM.performSearch':
                return {'searchId': 'search', 'resultCount': int('type=email' in params['query'])}
            if method == 'DOM.getSearchResults':
                return {'nodeIds': [2], 'nodes': [{'attributes': ['value', secret, 'name', 'private-name', 'id', 'private-id', 'placeholder', 'private-placeholder', 'aria-label', 'private-label']}]}
            if method == 'DOM.discardSearchResults':
                return {}
            if method == 'DOM.getBoxModel':
                return {'model': {'border': [10, 20, 110, 20, 110, 40, 10, 40]}}
            if method == 'DOM.getNodeForLocation':
                return {'nodeId': 2}
            raise AssertionError(method)

    monkeypatch.setattr(browser_text_fields, 'CdpClient', FakeCdp)
    async def no_frames(*_args):
        return []
    monkeypatch.setattr(browser_text_fields.browser_cdp_frames, 'child_frames', no_frames)
    reports = []
    async def read(width, height):
        return await browser_text_fields.probe_fields('test', width, height)
    watcher = TouchFields('thread-1', read, reports.append)
    task = asyncio.create_task(watcher.run())
    try:
        watcher.appearance({'width': 200, 'height': 100})
        watcher.frame('{"type":"frame","metadata":{"deviceWidth":200,"deviceHeight":100}}')
        await asyncio.sleep(0.3)
        assert reports == [] and calls == []
        watcher.appearance({'width': 200, 'height': 100, 'touch': True})
        async with asyncio.timeout(2):
            while not reports:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert reports == [{'type': 'text_fields', 'fields': [{'x': 10, 'y': 20, 'width': 100, 'height': 20, 'kind': 'email'}]}]
    encoded = json.dumps(reports)
    assert all(private not in encoded for private in (secret, 'private-name', 'private-id', 'private-placeholder', 'private-label'))
    assert set(calls) == {'DOM.getDocument', 'DOM.performSearch', 'DOM.getSearchResults', 'DOM.discardSearchResults', 'DOM.getBoxModel', 'DOM.getNodeForLocation'}


@pytest.mark.asyncio
async def test_animated_page_reports_fields_within_max_settle():
    from ws.browser_touch_fields import _MAX_SETTLE_SECONDS
    reports = []
    async def read(width, height):
        await asyncio.sleep(0.12)  # A frame arrives during the read too.
        return [{'x': 1, 'y': 2, 'width': width, 'height': height, 'kind': 'text'}], None
    watcher = TouchFields('thread-1', read, reports.append)
    watcher.appearance({'touch': True, 'width': 200, 'height': 100})
    task = asyncio.create_task(watcher.run())
    async def animate():
        while True:
            await asyncio.sleep(0.1)
            watcher.frame('frame data is not parsed')
    animation = asyncio.create_task(animate())
    try:
        async with asyncio.timeout(_MAX_SETTLE_SECONDS + 0.5):
            while not reports:
                await asyncio.sleep(0.01)
        assert reports[0]['fields'][0]['width'] == 200
    finally:
        animation.cancel()
        task.cancel()
        await asyncio.gather(animation, task, return_exceptions=True)


@pytest.mark.asyncio
async def test_page_focus_loss_closes_the_touch_target(monkeypatch):
    from ws import browser_touch_fields
    monkeypatch.setattr(browser_touch_fields, '_SETTLE_SECONDS', 0.001)
    focus: list[tuple[str, int] | None] = [('frame', 42)]
    reports = []
    async def read(_width, _height):
        return [], focus[0]
    watcher = TouchFields('thread-1', read, reports.append)
    watcher.appearance({'touch': True, 'width': 200, 'height': 100})
    task = asyncio.create_task(watcher.run())
    try:
        async with asyncio.timeout(2):
            while watcher.focused is None:
                await asyncio.sleep(0.001)
            focus[0] = None
            watcher.frame('{"type":"frame","metadata":{"deviceWidth":200,"deviceHeight":100}}')
            while {'type': 'text_field_blurred'} not in reports:
                await asyncio.sleep(0.001)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_closed_endpoint_leaves_the_touch_field_watch_running(monkeypatch):
    """A tab that goes away mid-read costs one read, never the pane's whole field watch."""
    from websockets.exceptions import ConnectionClosed

    from ws import browser_touch_fields
    monkeypatch.setattr(browser_touch_fields, '_SETTLE_SECONDS', 0.001)
    box = {'x': 1, 'y': 2, 'width': 3, 'height': 4, 'kind': 'text'}
    closes = [ConnectionClosed(None, None)]
    reads = 0
    reports: list[dict] = []

    async def read(_width, _height):
        nonlocal reads
        reads += 1
        if closes:
            raise closes.pop()
        return [box], None

    watcher = TouchFields('thread-1', read, reports.append)
    watcher.appearance({'touch': True, 'width': 200, 'height': 100})
    task = asyncio.create_task(watcher.run())
    try:
        async with asyncio.timeout(2):
            while reads < 1:
                await asyncio.sleep(0.001)
            assert not task.done()
            assert reports == []
            watcher.frame('frame data is not parsed')
            while not reports:
                await asyncio.sleep(0.001)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert reports == [{'type': 'text_fields', 'fields': [box]}]


@pytest.mark.asyncio
async def test_a_failed_read_reports_nothing_and_keeps_the_page_state(monkeypatch, caplog):
    """A read that fails tells nothing about the page: the keyboard stays open on the last known box."""
    from ws import browser_touch_fields
    monkeypatch.setattr(browser_touch_fields, '_SETTLE_SECONDS', 0.001)
    caplog.set_level(logging.WARNING, logger='ws.browser_touch_fields')
    box = {'x': 1, 'y': 2, 'width': 3, 'height': 4, 'kind': 'email'}
    focus: tuple[str, int] | None = ('frame', 42)
    failing = False
    reads = 0
    reports: list[dict] = []

    async def read(_width, _height):
        nonlocal reads
        reads += 1
        if failing:
            raise TimeoutError('the read never answered')
        return [box], focus

    watcher = TouchFields('thread-1', read, reports.append)
    watcher.appearance({'touch': True, 'width': 200, 'height': 100})
    task = asyncio.create_task(watcher.run())
    try:
        async with asyncio.timeout(2):
            while watcher.focused is None:
                await asyncio.sleep(0.001)
            reports.clear()
            failing = True
            watcher.frame('frame data is not parsed')
            while reads < 2:
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.01)
            assert reports == []
            assert watcher.focused == focus and watcher.last == [box]
            assert any('thread-1' in record.getMessage() and record.exc_info for record in caplog.records)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_an_editable_field_survives_a_hit_on_its_child(monkeypatch):
    """A rich-text editor's centre lands on the child the caret sits in, so its box still counts."""
    calls: list[tuple[str, object]] = []

    class FakeCdp:
        def __init__(self, _endpoint):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def call(self, method, **params):
            calls.append((method, params.get('nodeId')))
            if method == 'DOM.getDocument':
                return {'root': {'nodeId': 1}}
            if method == 'DOM.performSearch':
                return {'searchId': 'search', 'resultCount': int('contenteditable' in params['query'])}
            if method == 'DOM.getSearchResults':
                return {'nodeIds': [7]}
            if method == 'DOM.discardSearchResults':
                return {}
            if method == 'DOM.getBoxModel':
                return {'model': {'border': [10, 20, 110, 20, 110, 40, 10, 40]}}
            if method == 'DOM.getNodeForLocation':
                return {'nodeId': 8, 'backendNodeId': 88, 'frameId': 'frame'}
            if method == 'DOM.querySelectorAll':
                assert params == {'nodeId': 7, 'selector': '*'}
                return {'nodeIds': [8, 9]}
            raise AssertionError(method)

    monkeypatch.setattr(browser_text_fields, 'CdpClient', FakeCdp)
    async def no_frames(*_args):
        return []
    monkeypatch.setattr(browser_text_fields.browser_cdp_frames, 'child_frames', no_frames)
    fields, focused = await browser_text_fields.probe_fields('test', 200, 100)
    assert fields == [{'x': 10, 'y': 20, 'width': 100, 'height': 20, 'kind': 'editable'}]
    assert focused is None
    assert [call for call in calls if call[0] == 'DOM.querySelectorAll'] == [('DOM.querySelectorAll', 7)]


@pytest.mark.asyncio
async def test_a_plain_field_hit_on_another_node_is_still_dropped(monkeypatch):
    """The exact-node rule holds for every kind but an editable, so a covered field gets no box."""
    calls: list[str] = []

    class FakeCdp:
        def __init__(self, _endpoint):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def call(self, method, **params):
            calls.append(method)
            if method == 'DOM.getDocument':
                return {'root': {'nodeId': 1}}
            if method == 'DOM.performSearch':
                return {'searchId': 'search', 'resultCount': int('type=email' in params['query'])}
            if method == 'DOM.getSearchResults':
                return {'nodeIds': [7]}
            if method == 'DOM.discardSearchResults':
                return {}
            if method == 'DOM.getBoxModel':
                return {'model': {'border': [10, 20, 110, 20, 110, 40, 10, 40]}}
            if method == 'DOM.getNodeForLocation':
                return {'nodeId': 8, 'backendNodeId': 88, 'frameId': 'frame'}
            raise AssertionError(method)

    monkeypatch.setattr(browser_text_fields, 'CdpClient', FakeCdp)
    async def no_frames(*_args):
        return []
    monkeypatch.setattr(browser_text_fields.browser_cdp_frames, 'child_frames', no_frames)
    fields, _focused = await browser_text_fields.probe_fields('test', 200, 100)
    assert fields == []
    assert 'DOM.querySelectorAll' not in calls


@pytest.mark.asyncio
async def test_one_read_probes_at_most_the_bounded_number_of_nodes(monkeypatch):
    """A page of thousands of fields costs one bounded read, not thousands of serial round trips."""
    limit = browser_text_fields._PROBED_NODE_LIMIT
    matched = list(range(100, 100 + limit + 20))
    calls: list[str] = []
    probed: list[int] = []

    class FakeCdp:
        def __init__(self, _endpoint):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def call(self, method, **params):
            calls.append(method)
            if method == 'DOM.getDocument':
                return {'root': {'nodeId': 1}}
            if method == 'DOM.performSearch':
                return {'searchId': 'search', 'resultCount': len(matched) if 'type=email' in params['query'] else 0}
            if method == 'DOM.getSearchResults':
                return {'nodeIds': matched}
            if method == 'DOM.discardSearchResults':
                return {}
            if method == 'DOM.getBoxModel':
                probed.append(params['nodeId'])
                return {'model': {'border': [10, 20, 110, 20, 110, 40, 10, 40]}}
            if method == 'DOM.getNodeForLocation':
                return {'nodeId': probed[-1], 'backendNodeId': probed[-1], 'frameId': 'frame'}
            raise AssertionError(method)

    monkeypatch.setattr(browser_text_fields, 'CdpClient', FakeCdp)
    async def no_frames(*_args):
        return []
    monkeypatch.setattr(browser_text_fields.browser_cdp_frames, 'child_frames', no_frames)
    fields, _focused = await browser_text_fields.probe_fields('test', 200, 100)
    assert len(fields) == limit
    assert calls.count('DOM.getBoxModel') == limit
    assert calls.count('DOM.getNodeForLocation') == limit
