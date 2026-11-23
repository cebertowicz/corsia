"""Locate editable boxes without reading the page's values or text."""
from __future__ import annotations

from host_access import browser_cdp_frames
from host_access.browser_cdp import CdpClient, border_rect
from host_access.browser_session_state import BrowserSessionError

# Selectors classify without fetching any attributes, including value. Earlier kinds win.
_INPUT = 'input:not(:disabled):not([readonly])'
_SELECTORS = (
    ('password', f'{_INPUT}[type=password i]'),
    ('one-time-code', f'{_INPUT}[autocomplete~=one-time-code i]'),
    ('email', f'{_INPUT}[inputmode=email i], {_INPUT}[type=email i]'),
    ('tel', f'{_INPUT}[inputmode=tel i], {_INPUT}[type=tel i]'),
    ('url', f'{_INPUT}[inputmode=url i], {_INPUT}[type=url i]'),
    ('search', f'{_INPUT}[inputmode=search i], {_INPUT}[type=search i]'),
    ('number', f'{_INPUT}[inputmode=numeric i], {_INPUT}[inputmode=decimal i], {_INPUT}[type=number i]'),
    ('text', f'{_INPUT}:not([type]), ' + ', '.join(f'{_INPUT}[type={kind} i]' for kind in ('text', 'password', 'email', 'tel', 'url', 'search', 'number'))),
    ('textarea', 'textarea:not(:disabled):not([readonly])'),
    ('editable', '[contenteditable]:not([contenteditable=false i])'),
)

# One read asks Chrome for the box and the hit node of every matched node in every document, and a touch
# pane re-runs it after every frame pause. Measured on Chrome for Testing 152 (headless, product
# chrome_argv, own /tmp profile, 756x412 viewport, pages of 10 to 400 text inputs, median of 5 reads
# each, 2026-11-23): about 10 ms fixed plus 0.17 ms per matched node outside the viewport (box read only)
# and 0.32 ms per matched node inside it (box read plus hit test) — 82 ms for 400 nodes off screen,
# 132 ms with all 400 in view. DOM.getBoxModel is the only value-free source of a node's position, so
# nothing cheaper filters the page first; what is bounded is how many nodes one read examines. The read
# runs once per frame pause, whose quiet window is 250 ms, so a read must cost a fraction of that:
# 120 nodes keeps one near 50 ms. A page with more matched nodes than this gets boxes for the first 120
# in document order.
_PROBED_NODE_LIMIT = 120


async def probe_fields(endpoint: str, width: float, height: float) -> tuple[list[dict], tuple[object, object] | None]:
    """Return geometry and an internal focus identity, without fetching attributes or values."""
    async with CdpClient(endpoint) as cdp:
        fields: list[dict] = []
        focused: tuple[object, object] | None = None
        pending: list[tuple[browser_cdp_frames.Session, float, float, tuple[float, float, float, float]]] = [(cdp, 0, 0, (0, 0, width, height))]
        visited = 0
        budget = _PROBED_NODE_LIMIT
        while pending and budget > 0:
            session, x, y, clip = pending.pop()
            found, active, budget = await _document_fields(session, x, y, clip, budget)
            fields.extend(found)
            focused = active or focused
            for child, owner, _loader in await browser_cdp_frames.child_frames(cdp, session):
                try:
                    model = await session.call('DOM.getBoxModel', backendNodeId=owner)
                except BrowserSessionError:
                    continue
                quad = model.get('model', {}).get('content', [])
                if len(quad) >= 8:
                    frame_x, frame_y = quad[0], quad[1]
                    left, top = max(0, clip[0] - frame_x), max(0, clip[1] - frame_y)
                    right, bottom = min(quad[2] - frame_x, clip[2] - frame_x), min(quad[5] - frame_y, clip[3] - frame_y)
                    if right > left and bottom > top:
                        pending.append((child, x + frame_x, y + frame_y, (left, top, right, bottom)))
            visited += 1
            if visited > browser_cdp_frames.FRAME_LIMIT:
                break
        return fields, focused


async def _search(cdp: browser_cdp_frames.Session, query: str) -> list[int]:
    search = await cdp.call('DOM.performSearch', query=query, includeUserAgentShadowDOM=False)
    search_id = search.get('searchId')
    count = search.get('resultCount', 0)
    try:
        result = await cdp.call('DOM.getSearchResults', searchId=search_id, fromIndex=0, toIndex=count) if count else {}
    finally:
        await cdp.call('DOM.discardSearchResults', searchId=search_id)
    return [node for node in result.get('nodeIds', []) if isinstance(node, int)]


async def _contains(cdp: browser_cdp_frames.Session, node: int, hit: int | None) -> bool:
    """True when the hit node sits inside `node`. Ids only: DOM.querySelectorAll returns no attributes or text."""
    if not isinstance(hit, int):
        return False
    descendants = await cdp.call('DOM.querySelectorAll', nodeId=node, selector='*')
    return hit in (descendants.get('nodeIds') or [])


async def _document_fields(cdp: browser_cdp_frames.Session, offset_x: float, offset_y: float,
                           clip: tuple[float, float, float, float],
                           budget: int) -> tuple[list[dict], tuple[object, object] | None, int]:
    """Boxes inside `clip`, and the focus identity, examining at most `budget` matched nodes."""
    root = (await cdp.call('DOM.getDocument', depth=0)).get('root', {}).get('nodeId')
    if not isinstance(root, int):
        return [], None, budget
    fields: list[dict] = []
    active_nodes = set(await _search(cdp, ':focus'))
    focused: tuple[object, object] | None = None
    seen: set[int] = set()
    for kind, selector in _SELECTORS:
        for node in await _search(cdp, selector):
            if not isinstance(node, int) or node in seen:
                continue
            seen.add(node)
            if budget <= 0:
                return fields, focused, budget
            budget -= 1
            rect = await border_rect(cdp, nodeId=node)
            if rect is None:
                continue
            left, top = max(clip[0], rect['x']), max(clip[1], rect['y'])
            right = min(clip[2], rect['x'] + rect['width'])
            bottom = min(clip[3], rect['y'] + rect['height'])
            if right <= left or bottom <= top:
                continue
            hit = await cdp.call('DOM.getNodeForLocation', x=int((left + right) / 2), y=int((top + bottom) / 2), includeUserAgentShadowDOM=False)
            hit_node = hit.get('nodeId')
            # An editable's centre usually lands on the child paragraph the caret sits in, so a hit
            # anywhere inside it is the field; other kinds must be the node the box belongs to.
            if hit_node != node and not (kind == 'editable' and await _contains(cdp, node, hit_node)):
                continue
            fields.append({'x': offset_x + left, 'y': offset_y + top, 'width': right - left, 'height': bottom - top, 'kind': kind})
            if node in active_nodes:
                focused = (hit.get('frameId'), hit.get('backendNodeId'))
    return fields, focused, budget
