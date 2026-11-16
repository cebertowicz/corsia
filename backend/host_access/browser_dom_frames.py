"""Targets inside a child frame: agent-browser measures them from the frame's own corner, not the page's."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass

from host_access import browser_cdp, browser_dom_hit
from host_access.browser_cdp import CdpClient, Rect, RunCommand, protocol_int
from host_access.browser_dom_hit import (
    Located, cover_tag, in_viewport, is_empty, node_at_centre, node_for_box, page_viewport, read_box,
)
from host_access.browser_session_state import BrowserSessionError


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
                            frame_clients: AsyncExitStack) -> tuple[Located | None, str | None, bool]:
    """Find a target inside a child frame: the node, else the tag of what covers it, and whether a frame was tried.

    agent-browser measures an element inside a frame from that frame's own corner, so each frame's content-box
    origin moves the box onto the page. A frame counts only when the moved centre is inside both the page's
    viewport and the frame's own, and the page must hold still between the box and the hit test, as on the page.

    The search stops one level down: a frame nested inside a cross-site frame has its owner in that frame's
    process, not in this page, so it is never listed here and its elements are still refused.
    """
    cover: str | None = None
    for read in range(browser_dom_hit.SETTLE_READS):
        # The first box is the page path's last read; later ones are fresh, after the page moved.
        box = box if read == 0 else await read_box(run, selector)
        if box is None or is_empty(box):
            return None, None, False
        view = await page_viewport(page)
        shown = [(frame, _on_page(box, frame)) for frame in await _child_frames(page, endpoint)]
        shown = [(frame, placed) for frame, placed in shown if in_viewport(view, placed) and _inside(frame.viewport, placed)]
        if not shown:
            return None, None, False
        found, cover = None, None
        for frame, placed in shown:
            found, cover = await _hit_in_frame(page, frame, box, placed, frame_clients)
            if found is not None:
                break
        # The frame may have moved while it was hit, so the answer holds only if its place and the box did not.
        if await read_box(run, selector) == box and [frame.viewport for frame, _ in shown] == [
                await _owner_viewport(page, frame.owner) for frame, _ in shown]:
            return found, cover, True
        await asyncio.sleep(browser_dom_hit.SETTLE_INTERVAL)
    return None, None, False


async def _hit_in_frame(page: CdpClient, frame: ChildFrame, box: dict, placed: dict,
                        frame_clients: AsyncExitStack) -> tuple[Located | None, str | None]:
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
    target = await frame_clients.enter_async_context(CdpClient(frame.endpoint))
    node = await node_for_box(target, box)
    if node:
        return Located(node, target, None, (frame.viewport["x"], frame.viewport["y"])), None
    return None, await cover_tag(target, box)


async def _child_frames(page: CdpClient, endpoint: str) -> list[ChildFrame]:
    """Every frame whose owner element is in this page and shows a box, nearest the page first."""
    tree = await page.call("Page.getFrameTree")
    listed: list[tuple[str, str | None]] = [(frame_id, None) for frame_id in _descendants(tree.get("frameTree", {}))]
    # A cross-site frame is in no frame tree; it is a target of its own whose id is the frame's.
    listed += list((await asyncio.to_thread(browser_cdp.frame_targets, endpoint)).items())
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
