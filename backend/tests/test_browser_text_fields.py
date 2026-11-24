import asyncio
import json
import logging

import pytest

from host_access import browser_cdp_batch, browser_text_fields
from host_access.browser_session_state import BrowserSessionError
from ws.browser_touch_fields import TouchFields


class BatchingFake:
    """A fake page connection: `call` is the test's; a batch answers each entry through it, None where it fails."""

    session_id = None
    parents: dict[int, int] = {}

    async def call(self, method, **params):
        raise AssertionError(method)

    async def call_many(self, _session_id, method, params):
        results = []
        for one in params:
            try:
                results.append(await self.call(method, **one))
            except BrowserSessionError:
                results.append(None)
        return results


@pytest.mark.asyncio
async def test_only_field_geometry_and_kind_reaches_touch_pane(monkeypatch):
    secret = 'fixture-private-value'
    calls = []

    class FakeCdp(BatchingFake):
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
            if method == 'Page.getLayoutMetrics':
                return {}
            raise AssertionError(method)

    monkeypatch.setattr(browser_text_fields, 'BatchingCdpClient', FakeCdp)
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
    assert set(calls) == {'DOM.getDocument', 'DOM.performSearch', 'DOM.getSearchResults', 'DOM.discardSearchResults', 'Page.getLayoutMetrics',
                          'DOM.getBoxModel', 'DOM.getNodeForLocation'}


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
    """A rich-text editor's centre lands on the child the caret sits in, so its box still counts.

    The check walks the parent ids Chrome pushed with the hit, so it costs no call however large the editor is.
    """
    calls: list[tuple[str, object]] = []

    class FakeCdp(BatchingFake):
        # The hit (8) is a grandchild of the editor (7), through 12.
        parents = {8: 12, 12: 7, 7: 1}

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
            if method == 'Page.getLayoutMetrics':
                return {}
            raise AssertionError(method)

    monkeypatch.setattr(browser_text_fields, 'BatchingCdpClient', FakeCdp)
    async def no_frames(*_args):
        return []
    monkeypatch.setattr(browser_text_fields.browser_cdp_frames, 'child_frames', no_frames)
    fields, focused = await browser_text_fields.probe_fields('test', 200, 100)
    assert fields == [{'x': 10, 'y': 20, 'width': 100, 'height': 20, 'kind': 'editable'}]
    assert focused is None
    assert [method for method, _node in calls if method not in {'DOM.performSearch', 'DOM.getSearchResults', 'DOM.discardSearchResults'}] == [
        'DOM.getDocument', 'Page.getLayoutMetrics', 'DOM.getBoxModel', 'DOM.getNodeForLocation']


@pytest.mark.asyncio
async def test_scrolled_page_hit_tests_in_document_pixels_and_clips_to_its_viewport(monkeypatch):
    """Boxes are viewport pixels but the hit test takes document pixels, so a scrolled page adds its scroll.

    A pane reporting a larger size than the page shows gets no box past the page's own viewport.
    """
    hit_points: list[tuple[int, int]] = []
    # Node 7 sits in view; node 8 straddles the page's bottom edge at 300 css pixels.
    borders = {7: (10, 20, 110, 40), 8: (10, 290, 110, 310)}

    class FakeCdp(BatchingFake):
        def __init__(self, _endpoint):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def call(self, method, **params):
            if method == 'DOM.getDocument':
                return {'root': {'nodeId': 1}}
            if method == 'DOM.performSearch':
                return {'searchId': 'search', 'resultCount': int('type=email' in params['query'])}
            if method == 'DOM.getSearchResults':
                return {'nodeIds': [7, 8]}
            if method == 'DOM.discardSearchResults':
                return {}
            if method == 'Page.getLayoutMetrics':
                return {'cssVisualViewport': {'pageX': 5, 'pageY': 1000, 'clientWidth': 400, 'clientHeight': 300}}
            if method == 'DOM.getBoxModel':
                left, top, right, bottom = borders[params['nodeId']]
                return {'model': {'border': [left, top, right, top, right, bottom, left, bottom]}}
            if method == 'DOM.getNodeForLocation':
                hit_points.append((params['x'], params['y']))
                # Chrome answers in document pixels: only the scrolled point lands on a field.
                return {7: {'nodeId': 7}, 8: {'nodeId': 8}}.get({1030: 7, 1295: 8}.get(params['y']), {'nodeId': 1})
            raise AssertionError(method)

    monkeypatch.setattr(browser_text_fields, 'BatchingCdpClient', FakeCdp)
    async def no_frames(*_args):
        return []
    monkeypatch.setattr(browser_text_fields.browser_cdp_frames, 'child_frames', no_frames)
    # The pane reports twice the page's height.
    fields, _focused = await browser_text_fields.probe_fields('test', 400, 600)
    assert hit_points == [(65, 1030), (65, 1295)]
    assert fields == [{'x': 10, 'y': 20, 'width': 100, 'height': 20, 'kind': 'email'},
                      {'x': 10, 'y': 290, 'width': 100, 'height': 10, 'kind': 'email'}]


@pytest.mark.asyncio
async def test_in_view_fields_get_boxes_behind_many_off_screen_fields(monkeypatch):
    """Fields in view get boxes even when many off-screen fields match an earlier kind."""
    off_screen = list(range(1000, 1300))
    in_view = [7, 8, 9]
    selectors = dict(browser_text_fields._SELECTORS)
    # Off-screen fields match an earlier kind than the ones in view.
    results = {selectors['password']: off_screen, selectors['text']: in_view}

    class FakeCdp(BatchingFake):
        def __init__(self, _endpoint):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def call(self, method, **params):
            if method == 'DOM.getDocument':
                return {'root': {'nodeId': 1}}
            if method == 'DOM.performSearch':
                return {'searchId': params['query'], 'resultCount': len(results.get(params['query'], []))}
            if method == 'DOM.getSearchResults':
                return {'nodeIds': results[params['searchId']]}
            if method == 'DOM.discardSearchResults':
                return {}
            if method == 'DOM.getBoxModel':
                node = params['nodeId']
                left, top = (10, 2000) if node in off_screen else (10 + 50 * in_view.index(node), 20)
                return {'model': {'border': [left, top, left + 40, top, left + 40, top + 20, left, top + 20]}}
            if method == 'DOM.getNodeForLocation':
                return {'nodeId': in_view[(params['x'] - 10) // 50]}
            if method == 'Page.getLayoutMetrics':
                return {}
            raise AssertionError(method)

    monkeypatch.setattr(browser_text_fields, 'BatchingCdpClient', FakeCdp)
    async def no_frames(*_args):
        return []
    monkeypatch.setattr(browser_text_fields.browser_cdp_frames, 'child_frames', no_frames)
    fields, _focused = await browser_text_fields.probe_fields('test', 200, 100)
    assert [(field['x'], field['kind']) for field in fields] == [(10, 'text'), (60, 'text'), (110, 'text')]


@pytest.mark.asyncio
async def test_a_batch_reads_every_reply_and_keeps_the_events_between_them(caplog):
    """call_many sends the whole batch, then answers each entry, None where one failed, recording pushed parents.

    A failed entry is logged with its CDP code and message, as call_in logs one.
    """
    caplog.set_level(logging.DEBUG, logger=browser_cdp_batch.__name__)
    sent: list[dict] = []

    class Socket:
        def __init__(self):
            self.replies = iter([
                {'method': 'DOM.setChildNodes', 'params': {'parentId': 1, 'nodes': [{'nodeId': 2}]}},
                {'id': 1, 'error': {'code': -32000, 'message': 'Could not compute box model.'}},
                {'id': 2, 'result': {'model': {'border': [0, 0, 1, 0, 1, 1, 0, 1]}}},
            ])

        async def send(self, data):
            sent.append(json.loads(data))

        async def recv(self):
            assert len(sent) == 2, 'every command goes out before the first reply is read'
            return json.dumps(next(self.replies))

    client = browser_cdp_batch.BatchingCdpClient('ws://fixture')
    client.socket = Socket()
    results = await client.call_many(None, 'DOM.getBoxModel', [{'nodeId': 5}, {'nodeId': 6}])
    assert results == [None, {'model': {'border': [0, 0, 1, 0, 1, 1, 0, 1]}}]
    assert [message['params'] for message in sent] == [{'nodeId': 5}, {'nodeId': 6}]
    assert client.parents == {2: 1}
    assert 'DOM.getBoxModel' in caplog.text and '-32000' in caplog.text and 'Could not compute box model.' in caplog.text


@pytest.mark.asyncio
async def test_a_plain_field_hit_on_another_node_is_still_dropped(monkeypatch):
    """The exact-node rule holds for every kind but an editable, so a covered field gets no box."""
    calls: list[str] = []

    class FakeCdp(BatchingFake):
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
            if method == 'Page.getLayoutMetrics':
                return {}
            raise AssertionError(method)

    monkeypatch.setattr(browser_text_fields, 'BatchingCdpClient', FakeCdp)
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

    class FakeCdp(BatchingFake):
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
                # Every box is the same, so the nth hit test is the nth box read's node.
                node = probed[calls.count('DOM.getNodeForLocation') - 1]
                return {'nodeId': node, 'backendNodeId': node, 'frameId': 'frame'}
            if method == 'Page.getLayoutMetrics':
                return {}
            raise AssertionError(method)

    monkeypatch.setattr(browser_text_fields, 'BatchingCdpClient', FakeCdp)
    async def no_frames(*_args):
        return []
    monkeypatch.setattr(browser_text_fields.browser_cdp_frames, 'child_frames', no_frames)
    fields, _focused = await browser_text_fields.probe_fields('test', 200, 100)
    assert len(fields) == limit
    assert calls.count('DOM.getBoxModel') == limit
    assert calls.count('DOM.getNodeForLocation') == limit
