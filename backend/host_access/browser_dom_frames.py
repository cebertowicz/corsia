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

# Whether a document holds an element with the target's box, whatever covers it and wherever it sits: every
# element, through open shadow roots, so one scrolled out of the document's own view is counted too.
_HOLDS = """function(box) {
  const near = (rect) => ['x', 'y', 'width', 'height'].every((key) => Math.abs(rect[key] - box[key]) <= 1);
  const holds = (root) => Array.from(root.querySelectorAll('*')).some((element) =>
    near(element.getBoundingClientRect()) || (element.shadowRoot !== null && holds(element.shadowRoot)));
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
    and each frame that hold an element with that box, shown or not, since a look-alike embed below the fold,
    or scrolled out of its own frame's view, names it as well as one in view. Why not is "ambiguous" when more
    than one does, "covered" when the one that does is a shown frame whose hit test reached something else, and
    None when no frame holds it or the one that does doesn't show it and can't be hit, so the page's own answer
    stands. A document that cannot be read counts as holding it, so a doubt refuses. A covered element inside a
    closed shadow root is not seen.

    agent-browser measures an element inside a frame from that frame's own corner, so each frame's content-box
    origin moves the box onto the page. A frame shows the box, and is hit, only when the moved centre is inside
    both the frame's own viewport and the page's, and the page must hold still between the box and the hit
    test, as on the page.

    Only the frames whose owner element is in this page are hit. A frame nested inside a cross-site frame has its
    owner in that frame's process, so its elements are still refused; but it is a document an agent's ref can
    name, so one holding an element with the box makes a look-alike nearer the page ambiguous.
    """
    for read in range(browser_dom_hit.SETTLE_READS):
        # The first box is the page path's last read; later ones are fresh, after the page moved.
        box = box if read == 0 else await read_box(run, selector)
        if box is None or is_empty(box):
            return None, None, None
        view = await page_viewport(page)
        frames = await _child_frames(page, endpoint)
        if not frames:
            return None, None, None
        held = []
        for frame in frames:
            placed = _on_page(box, frame)
            shown = _shows(view, frame.viewport, box)
            located, cover = await _hit_in_frame(page, frame, box, placed, targets) if shown else (None, None)
            if located is not None or await _frame_holds(page, frame, box, targets):
                held.append((located, cover, shown))
        for cdp, frame_id in await _nested_documents(frames, endpoint, targets):
            if cdp is None or await _holds(cdp, frame_id, box):
                held.append((None, None, False))
        page_holds = in_viewport(view, box) and await _holds(page, None, box)
        # A frame may have moved while it was hit, so the answer holds only if its place and the box did not.
        if await read_box(run, selector) == box and [_place(view, frame.viewport, box) for frame in frames] == [
                _place(view, await _owner_viewport(page, frame.owner), box) for frame in frames]:
            if len(held) + page_holds > 1:
                # Two places laid out alike both hold an element that matches the box, and which one the agent
                # named can't be told, whichever of them is covered: refused, as before frames were searched.
                return None, "ambiguous", None
            if not held:
                return None, None, None
            located, cover, shown = held[0]
            if located is not None:
                return located, None, None
            # A frame that doesn't show the box can't be hit: refused for the page's own reason, as before frames
            # were searched.
            return (None, "covered", cover) if shown else (None, None, None)
        await asyncio.sleep(browser_dom_hit.SETTLE_INTERVAL)
    return None, None, None


def _shows(view: dict, viewport: Rect, box: dict) -> bool:
    """Whether a frame shows a box measured from its corner: inside its own viewport and the page's."""
    placed = {**box, "x": box["x"] + viewport["x"], "y": box["y"] + viewport["y"]}
    return _inside(viewport, placed) and in_viewport(view, placed)


def _place(view: dict, viewport: Rect | None, box: dict) -> object:
    """What of a frame's place must hold still: all of it when it shows the box; otherwise only its size.

    A frame that doesn't show the box is never hit, so where it sits doesn't change whether it holds the
    element; a carousel or ad below the fold that keeps moving must not refuse a write to the frame in view.
    """
    if viewport is None:
        return None
    return viewport if _shows(view, viewport, box) else (viewport["width"], viewport["height"])


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
            arguments=[{"value": {key: box[key] for key in ("x", "y", "width", "height")}}],
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


async def _nested_documents(frames: list[ChildFrame], endpoint: str,
                            targets: FrameTargets) -> list[tuple[CdpClient | None, str | None]]:
    """Every document below a cross-site frame, as the target it is read through and the frame to name in it.

    Such a document is in the cross-site frame's own process (in that target's frame tree), or in a target of its
    own whose owner element is in a frame above it. A frame whose target can't be read is skipped: it already
    counts as holding the element, so a doubt refuses. One found deeper that can't be read counts the same way.
    """
    found: list[tuple[CdpClient | None, str | None]] = []
    parents = [frame.endpoint for frame in frames if frame.endpoint is not None]
    if not parents:
        return found
    unplaced = {frame_id: frame_endpoint for frame_id, frame_endpoint in
                (await asyncio.to_thread(frame_targets, endpoint)).items()
                if frame_id not in {frame.id for frame in frames}}
    while parents:
        try:
            parent = await targets.open(parents.pop())
            tree = await parent.call("Page.getFrameTree")
        except BrowserSessionError:
            continue
        found += [(parent, frame_id) for frame_id in _descendants(tree.get("frameTree", {}))]
        for frame_id, frame_endpoint in list(unplaced.items()):
            try:
                await parent.call("DOM.getFrameOwner", frameId=frame_id)
            except BrowserSessionError:
                # Its owner is elsewhere: deeper still, or in another tab.
                continue
            del unplaced[frame_id]
            try:
                found.append((await targets.open(frame_endpoint), None))
            except BrowserSessionError:
                found.append((None, None))
                continue
            parents.append(frame_endpoint)
    return found


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
