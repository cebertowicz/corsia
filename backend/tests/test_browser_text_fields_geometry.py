"""Where the touch pane's field boxes land: scroll, viewport and the per-read node bound."""
import pytest

from host_access import browser_text_fields
from host_access.browser_session_state import BrowserSessionError
from tests.browser_text_fields_fakes import UNSCROLLED, BatchingFake


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
                return UNSCROLLED
            raise AssertionError(method)

    monkeypatch.setattr(browser_text_fields, 'BatchingCdpClient', FakeCdp)
    async def no_frames(*_args):
        return []
    monkeypatch.setattr(browser_text_fields.browser_cdp_frames, 'child_frames', no_frames)
    fields, _focused = await browser_text_fields.probe_fields('test', 200, 100)
    assert len(fields) == limit
    assert calls.count('DOM.getBoxModel') == limit
    assert calls.count('DOM.getNodeForLocation') == limit


@pytest.mark.asyncio
@pytest.mark.parametrize('metrics', [{}, {'cssVisualViewport': None},
                                     {'cssVisualViewport': {'pageX': 0, 'pageY': 0, 'clientWidth': 400}},
                                     {'cssVisualViewport': {'pageX': 0, 'pageY': True, 'clientWidth': 400, 'clientHeight': 300}}])
async def test_a_page_whose_layout_metrics_lack_its_scroll_or_viewport_fails_the_read(monkeypatch, metrics):
    """No point can be placed without them; guessing an unscrolled page drops every box on a scrolled one."""
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
            if method == 'Page.getLayoutMetrics':
                return metrics
            raise AssertionError(method)

    monkeypatch.setattr(browser_text_fields, 'BatchingCdpClient', FakeCdp)
    with pytest.raises(BrowserSessionError, match="This page couldn't be checked"):
        await browser_text_fields.probe_fields('test', 400, 300)
    assert 'DOM.getBoxModel' not in calls and 'DOM.getNodeForLocation' not in calls
