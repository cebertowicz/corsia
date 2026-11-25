"""Where an agent's target is on a page and what a hit test finds there, shared by the page and frame write paths."""
from __future__ import annotations

import re
from dataclasses import dataclass

from host_access import browser_cli
from host_access.browser_cdp import RunCommand, border_rect, protocol_int
from host_access.browser_cdp_frames import PAGE_CHECK_FAILED, Session
from host_access.browser_session_state import BrowserSessionError

# A page that is still moving (a smooth scroll that scrollintoview started and did not wait for)
# is re-read at this interval, for at most this many reads, until a hit test holds. Each read also
# runs a box command and a hit test, so the wait is longer than reads times interval.
SETTLE_INTERVAL = 0.05
SETTLE_READS = 40


@dataclass(frozen=True)
class Located:
    """The node a write acts on, and where to reach it.

    ``cdp`` is the target the node lives in: the page, or a cross-site frame's own target. ``frame_id`` names
    the in-page frame whose world the write runs in (None for the target's main frame). ``shift`` is the
    frame origin to add to a rect measured by ``cdp`` for it to be in the page's pixels.
    """

    node: int
    cdp: Session
    frame_id: str | None = None
    shift: tuple[float, float] = (0, 0)


async def read_box(run: RunCommand, selector: str) -> dict | None:
    result = await run(("get", "box", selector), 10)
    payload = browser_cli.json_object(result.stdout)
    data = payload.get("data")
    # agent-browser 0.31 exposes box geometry directly in data (older verified builds used data.result).
    box = data.get("result", data) if isinstance(data, dict) else None
    if not isinstance(box, dict) or not all(isinstance(box.get(key), (int, float)) for key in ("x", "y", "width", "height")):
        return None
    return box


async def page_viewport(cdp: Session) -> dict:
    return (await cdp.call("Page.getLayoutMetrics")).get("cssVisualViewport", {})


def is_empty(box: dict) -> bool:
    """The all-zero box agent-browser gives for an element with no layout; one zero side is a real element."""
    return not any(box[key] for key in ("x", "y", "width", "height"))


def in_viewport(viewport: dict, box: dict) -> bool:
    width, height = viewport.get("clientWidth"), viewport.get("clientHeight")
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
        return False
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    return 0 <= x < width and 0 <= y < height


async def node_for_box(cdp: Session, box: dict, *, strict: bool = False, max_ancestors: int | None = None) -> int | None:
    node = await node_at_centre(cdp, box, strict=strict)
    return await matching_ancestor(cdp, node, box, strict=strict, max_ancestors=max_ancestors) if node else None


async def node_at_centre(cdp: Session, box: dict, *, strict: bool = False) -> int | None:
    # The box is in viewport pixels, but the hit test takes document pixels: add the scroll offset.
    viewport = await page_viewport(cdp)
    scroll_x, scroll_y = viewport.get("pageX"), viewport.get("pageY")
    if not isinstance(scroll_x, (int, float)) or not isinstance(scroll_y, (int, float)):
        if strict:
            raise BrowserSessionError(PAGE_CHECK_FAILED)
        return None
    try:
        # Hit-test as a real click does: a layer with pointer-events: none (a toast or overlay container
        # over the whole page) lets the click through, so it does not cover what lies beneath it.
        location = await cdp.call("DOM.getNodeForLocation", x=round(scroll_x + box["x"] + box["width"] / 2),
                                  y=round(scroll_y + box["y"] + box["height"] / 2), includeUserAgentShadowDOM=False,
                                  ignorePointerEventsNone=False)
    except BrowserSessionError:
        # Chrome reports no node at a point nothing hit-tests at.
        if strict:
            raise
        return None
    raw_node = location.get("backendNodeId")
    node = protocol_int(raw_node)
    if strict and (isinstance(raw_node, bool) or node <= 0):
        raise BrowserSessionError(PAGE_CHECK_FAILED)
    return node


async def cover_tag(cdp: Session, box: dict | None, *, strict: bool = False) -> str | None:
    """The tag of the element at the target's centre, so the agent can dismiss or scroll past it."""
    node = await node_at_centre(cdp, box, strict=strict) if box is not None and not is_empty(box) else None
    if not node:
        return None
    try:
        name = (await cdp.call("DOM.describeNode", backendNodeId=node)).get("node", {}).get("nodeName")
    except BrowserSessionError:
        if strict:
            raise
        return None
    # Only a tag name: page text, ids and classes stay out of the agent's error.
    return name.lower() if isinstance(name, str) and re.fullmatch(r"[A-Za-z][\w-]*", name) else None


async def matching_ancestor(cdp: Session, node: int, expected: dict, *, strict: bool = False,
                            max_ancestors: int | None = None) -> int | None:
    """Walk from the hit to a matching ancestor, including shadow hosts.

    Refs expose geometry but not identity; a same-sized overlay can still match.
    """
    context = None
    steps = 0
    while node:
        steps += 1
        if max_ancestors is not None and steps > max_ancestors:
            raise BrowserSessionError(PAGE_CHECK_FAILED)
        if await box_matches(cdp, node, expected, strict=strict):
            return node
        try:
            if context is None:
                context = await isolated_context(cdp)
            resolved = await cdp.call("DOM.resolveNode", backendNodeId=node, executionContextId=context)
            parent = await cdp.call(
                "Runtime.callFunctionOn", objectId=remote_object_id(resolved), returnByValue=False, silent=True,
                functionDeclaration="function() { return this.parentElement || this.getRootNode().host || null; }",
            )
            object_id = parent.get("result", {}).get("objectId")
            if not isinstance(object_id, str):
                return None
            description = await cdp.call("DOM.describeNode", objectId=object_id)
            node = protocol_int(description.get("node", {}).get("backendNodeId"))
        except BrowserSessionError:
            # An inaccessible frame kept the old refusal when its hit box did not match.
            if strict:
                raise
            return None
    return None


async def box_matches(cdp: Session, node: int, expected: dict, *, strict: bool = False) -> bool:
    rect = await border_rect(cdp, backendNodeId=node, strict=strict)
    return rect is not None and all(abs(rect[key] - expected[key]) <= 1 for key in ("x", "y", "width", "height"))


async def isolated_context(cdp: Session, frame_id: str | None = None) -> int:
    """A world of ours in a frame: the target's main frame, or the in-page frame named."""
    if frame_id is None:
        tree = await cdp.call("Page.getFrameTree")
        frame = tree.get("frameTree", {}).get("frame", {}).get("id")
    else:
        frame = frame_id
    world = await cdp.call("Page.createIsolatedWorld", frameId=frame, worldName="corsia-shared-browser", grantUniveralAccess=False)
    return protocol_int(world.get("executionContextId"))


def remote_object_id(result: dict) -> str:
    value = result.get("object")
    if not isinstance(value, dict) or not isinstance(value.get("objectId"), str):
        raise BrowserSessionError("This page couldn't be checked")
    return value["objectId"]
