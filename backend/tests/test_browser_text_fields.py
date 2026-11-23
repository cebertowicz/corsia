import asyncio
import json

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
    watcher = TouchFields(read, reports.append)
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
async def test_page_focus_loss_closes_the_touch_target(monkeypatch):
    from ws import browser_touch_fields
    monkeypatch.setattr(browser_touch_fields, '_SETTLE_SECONDS', 0.001)
    focus: list[tuple[str, int] | None] = [('frame', 42)]
    reports = []
    async def read(_width, _height):
        return [], focus[0]
    watcher = TouchFields(read, reports.append)
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
