"""Targets inside a child frame: agent-browser measures them from the frame's own corner, not the page's."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass

from websockets.exceptions import WebSocketException

from host_access import browser_cdp, browser_dom_hit
from host_access.browser_cdp import CdpClient, Rect, RunCommand, protocol_int
from host_access.browser_dom_hit import (
    Located, cover_tag, in_viewport, is_empty, isolated_context, node_at_centre, node_for_box, page_viewport, read_box,
)
from host_access.browser_session_state import BrowserSessionError

# Whether a document holds an element with the target's box at its place, whatever covers it: every element at
# the point, through open shadow roots, not only the topmost one a hit test returns.
_HOLDS = """function(x, y, box) {
  const near = (rect) => ['x', 'y', 'width', 'height'].every((key) => Math.abs(rect[key] - box[key]) <= 1);
  const seen = new Set();
  const holds = (root) => root.elementsFromPoint(x, y).some((element) => {
    if (seen.has(element)) return false;
    seen.add(element);
    return near(element.getBoundingClientRect()) || (element.shadowRoot !== null && holds(element.shadowRoot));
  });
  return holds(document);
}"""


class FrameTargets:
    """The cross-site frames' own targets one write opens, each opened once and closed when the write returns."""

    def __init__(self, stack: AsyncExitStack) -> None:
        self._stack = stack
        self._open: dict[str, CdpClient] = {}

    async def open(self, endpoint: str) -> CdpClient:
        if endpoint not in self._open:
            try:
                self._open[endpoint] = await self._stack.enter_async_context(CdpClient(endpoint))
            except (OSError, TimeoutError, WebSocketException) as exc:
                # A frame can go away between being listed and being opened (an ad frame swapped out).
                raise BrowserSessionError("This page couldn't be checked") from exc
        return self._open[endpoint]


@dataclass(frozen=True)
class ChildFrame:
    """A frame below the page: its id, its owner element in the page, where its viewport shows on the page.

    ``endpoint`` is the frame's own target when Chrome runs it in another process (a cross-site frame).
    """

    id: str
    owner: int
    viewport: Rect
    endpoint: str | None = None


async def locate_in_frames(run: RunCommand, page: CdpClient, endpoint: str, selector: str, box: dict | None,
                            targets: FrameTargets) -> tuple[Located | None, str | None, str | None]:
    """Find a target inside a child frame: the node, else why not, and the tag of what covers it.

    A ref gives only a box, so which document it names is decided first, apart from what covers it: the page
    and each frame that hold an element with that box at its place, on screen or not, since a look-alike embed
    below the fold names it as well as one in view. Why not is "ambiguous" when more than one does, "covered"
    when the one that does is a frame on screen whose hit test reached something else, and None when no frame
    holds it or the one that does is off screen and can't be hit, so the page's own answer stands. A document
    that cannot be read counts as holding it, so a doubt refuses. A covered element inside a closed shadow root
    is not seen.

    agent-browser measures an element inside a frame from that frame's own corner, so each frame's content-box
    origin moves the box onto the page. A frame counts only when the moved centre is inside the frame's own
    viewport, is hit only when it is also inside the page's, and the page must hold still between the box and
    the hit test, as on the page.

    The search stops one level down: a frame nested inside a cross-site frame has its owner in that frame's
    process, not in this page, so it is never listed here and its elements are still refused.
    """
    for read in range(browser_dom_hit.SETTLE_READS):
        # The first box is the page path's last read; later ones are fresh, after the page moved.
        box = box if read == 0 else await read_box(run, selector)
        if box is None or is_empty(box):
            return None, None, None
        view = await page_viewport(page)
        placed_in = [(frame, _on_page(box, frame)) for frame in await _child_frames(page, endpoint)]
        placed_in = [(frame, placed) for frame, placed in placed_in if _inside(frame.viewport, placed)]
        if not placed_in:
            return None, None, None
        held = []
        for frame, placed in placed_in:
            on_screen = in_viewport(view, placed)
            located, cover = await _hit_in_frame(page, frame, box, placed, targets) if on_screen else (None, None)
            if located is not None or await _frame_holds(page, frame, box, targets):
                held.append((located, cover, on_screen))
        page_holds = in_viewport(view, box) and await _holds(page, None, box)
        # A frame may have moved while it was hit, so the answer holds only if its place and the box did not.
        if await read_box(run, selector) == box and [_place(view, frame.viewport, box) for frame, _ in placed_in] == [
                _place(view, await _owner_viewport(page, frame.owner), box) for frame, _ in placed_in]:
            if len(held) + page_holds > 1:
                # Two places laid out alike both hold an element that matches the box, and which one the agent
                # named can't be told, whichever of them is covered: refused, as before frames were searched.
                return None, "ambiguous", None
            if not held:
                return None, None, None
            located, cover, on_screen = held[0]
            if located is not None:
                return located, None, None
            # A frame off screen can't be hit: refused for the page's own reason, as before frames were searched.
            return (None, "covered", cover) if on_screen else (None, None, None)
        await asyncio.sleep(browser_dom_hit.SETTLE_INTERVAL)
    return None, None, None


def _place(view: dict, viewport: Rect | None, box: dict) -> object:
    """What of a frame's place must hold still: all of it on screen; off screen, only its size and staying off.

    An off-screen frame is never hit, so where it sits doesn't change whether it holds the element; a carousel
    or ad below the fold that keeps moving must not refuse a write to the frame in view.
    """
    if viewport is None:
        return None
    on_screen = in_viewport(view, {**box, "x": box["x"] + viewport["x"], "y": box["y"] + viewport["y"]})
    return viewport if on_screen else (viewport["width"], viewport["height"])


async def _frame_holds(page: CdpClient, frame: ChildFrame, box: dict, targets: FrameTargets) -> bool:
    """Whether a frame's document holds an element with the box at its place; a frame that can't be opened might."""
    try:
        return await _holds(*(await _document(page, frame, targets)), box)
    except (BrowserSessionError, OSError, TimeoutError, WebSocketException):
        return True


async def _document(page: CdpClient, frame: ChildFrame, targets: FrameTargets) -> tuple[CdpClient, str | None]:
    """The target a frame's document is read through, and the frame to name in it (None for its main frame)."""
    return (page, frame.id) if frame.endpoint is None else (await targets.open(frame.endpoint), None)


async def _holds(cdp: CdpClient, frame_id: str | None, box: dict) -> bool:
    """Whether a document holds an element with the box at its place, whatever covers it; unreadable counts as yes."""
    try:
        result = await cdp.call(
            "Runtime.callFunctionOn", executionContextId=await isolated_context(cdp, frame_id), functionDeclaration=_HOLDS,
            arguments=[{"value": box["x"] + box["width"] / 2}, {"value": box["y"] + box["height"] / 2},
                       {"value": {key: box[key] for key in ("x", "y", "width", "height")}}],
            returnByValue=True, silent=True)
    except BrowserSessionError:
        return True
    value = result.get("result", {}).get("value")
    return "exceptionDetails" in result or value is not False


async def _hit_in_frame(page: CdpClient, frame: ChildFrame, box: dict, placed: dict,
                        targets: FrameTargets) -> tuple[Located | None, str | None]:
    """The node a box inside a frame names, or the tag of what covers it."""
    if frame.endpoint is None:
        # A frame in the page's own process is hit-tested through the page, at the box moved onto the page.
        node = await node_for_box(page, placed)
        if node:
            return Located(node, page, frame.id), None
        return None, await cover_tag(page, placed)
    # A cross-site frame shows on the page as its owner element; what is inside it is known only to its own target.
    if await node_at_centre(page, placed) != frame.owner:
        return None, await cover_tag(page, placed)
    target = await targets.open(frame.endpoint)
    node = await node_for_box(target, box)
    if node:
        return Located(node, target, None, (frame.viewport["x"], frame.viewport["y"])), None
    return None, await cover_tag(target, box)


def frame_targets(endpoint: str) -> dict[str, str]:
    """The frames Chrome runs in their own processes, as frame id to that target's endpoint.

    Such a frame is not in its parent page's frame tree; Chrome lists it beside ``endpoint`` as a target
    of its own whose id is the frame id. A host that answers nothing has none as far as this sees, so a
    page that cannot be listed keeps its caller's own refusal instead of reporting the listing's failure.
    """
    try:
        targets = browser_cdp.list_targets(endpoint)
    except BrowserSessionError:
        return {}
    return {target["id"]: target["webSocketDebuggerUrl"] for target in targets
            if target.get("type") == "iframe" and isinstance(target.get("id"), str)}


async def _child_frames(page: CdpClient, endpoint: str) -> list[ChildFrame]:
    """Every frame whose owner element is in this page and shows a box, nearest the page first."""
    tree = await page.call("Page.getFrameTree")
    listed: list[tuple[str, str | None]] = [(frame_id, None) for frame_id in _descendants(tree.get("frameTree", {}))]
    # A cross-site frame is in no frame tree; it is a target of its own whose id is the frame's.
    listed += list((await asyncio.to_thread(frame_targets, endpoint)).items())
    frames = []
    for frame_id, frame_endpoint in listed:
        try:
            owner = protocol_int((await page.call("DOM.getFrameOwner", frameId=frame_id)).get("backendNodeId"))
            viewport = await _owner_viewport(page, owner)
        except BrowserSessionError:
            # A frame of another tab, or one with no element in this page.
            continue
        if viewport is not None:
            frames.append(ChildFrame(frame_id, owner, viewport, frame_endpoint))
    return frames


async def _owner_viewport(page: CdpClient, owner: int) -> Rect | None:
    """Where a frame's own viewport shows on the page: its owner element's content box."""
    try:
        content = (await page.call("DOM.getBoxModel", backendNodeId=owner)).get("model", {}).get("content")
    except BrowserSessionError:
        return None
    if not isinstance(content, list) or len(content) < 8:
        return None
    viewport = {"x": content[0], "y": content[1], "width": content[2] - content[0], "height": content[5] - content[1]}
    return viewport if viewport["width"] > 0 and viewport["height"] > 0 else None


def _descendants(node: dict) -> list[str]:
    found = []
    for child in node.get("childFrames", []) or []:
        frame_id = child.get("frame", {}).get("id")
        if isinstance(frame_id, str):
            found.append(frame_id)
        found.extend(_descendants(child))
    return found


def _on_page(box: dict, frame: ChildFrame) -> dict:
    """A box measured from the frame's corner, moved to where it shows on the page."""
    return {**box, "x": box["x"] + frame.viewport["x"], "y": box["y"] + frame.viewport["y"]}


def _inside(viewport: Rect, box: dict) -> bool:
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    return viewport["x"] <= x < viewport["x"] + viewport["width"] and viewport["y"] <= y < viewport["y"] + viewport["height"]


def shifted(rect: Rect, shift: tuple[float, float]) -> Rect:
    return {**rect, "x": rect["x"] + shift[0], "y": rect["y"] + shift[1]}
