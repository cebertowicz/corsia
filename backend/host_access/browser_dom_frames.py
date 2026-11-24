"""Locate frame-relative boxes using the page's flattened CDP sessions.

One action walks at most FRAME_LIMIT documents once and scans each document at most once for a box:
64 documents × 25,000 elements × 1 scan, plus at most two frame hit-test passes, each walking at
most 25,000 ancestors per frame. A scan over the cap counts as a possible match.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from math import isfinite

from websockets.exceptions import WebSocketException

from host_access import browser_cdp_frames
from host_access.browser_cdp import CdpClient, Rect, RunCommand, protocol_int
from host_access.browser_cdp_frames import PAGE_CHECK_FAILED, Session
from host_access.browser_dom_hit import (
    Located, cover_tag, in_viewport, is_empty, isolated_context, node_at_centre, node_for_box, page_viewport, read_box,
)
from host_access.browser_session_state import BrowserSessionError

# The live Wikipedia United States article had 21,865 elements in Chrome on 2026-11-24
# (https://en.wikipedia.org/wiki/United_States). Allow that heavy page with some headroom.
_HOLDS_LIMIT = 25000
_HOLDS = """function(box, limit) {
  // Avoid querySelectorAll('*'): it materializes every element before a limit can be applied.
  const near = (rect) => ['x', 'y', 'width', 'height'].every((key) => Math.abs(rect[key] - box[key]) <= 1);
  const roots = [document];
  let count = 0;
  while (roots.length) {
    const walker = document.createTreeWalker(roots.pop(), NodeFilter.SHOW_ELEMENT);
    let element;
    while ((element = walker.nextNode())) {
      if (++count > limit || near(element.getBoundingClientRect())) return true;
      if (element.shadowRoot !== null) roots.push(element.shadowRoot);
    }
  }
  return false;
}"""


@dataclass(frozen=True)
class ChildFrame:
    """A document, the session in which its owner sits, and its place in that session."""

    session: Session
    frame_id: str | None
    parent: Session
    owner: int
    viewport: Rect | None
    direct: bool
    cross_process: bool


@dataclass(frozen=True)
class FrameWalk:
    frames: list[ChildFrame]
    attachment_count: int
    trees: list[tuple[Session, dict]] = field(default_factory=list)


async def walk_frames(page: CdpClient) -> FrameWalk:
    try:
        return await _walk_frames(page)
    except (OSError, TimeoutError, WebSocketException, AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise BrowserSessionError(PAGE_CHECK_FAILED) from exc


async def _walk_frames(page: CdpClient) -> FrameWalk:
    """Collect documents once, using the password probe's flattened-session attachment walk.

    Same-process documents are in their session's frame tree; a child target's owner is in its
    parent session. A frame tree, attachment, owner or viewport read which fails refuses the write.
    """
    frames: list[ChildFrame] = []
    trees: list[tuple[Session, dict]] = []
    owners: dict[tuple[str | None, int], Rect | None] = {}
    document_count = 1

    async def collect(session: Session) -> set[int]:
        nonlocal document_count
        tree = (await session.call("Page.getFrameTree")).get("frameTree")
        ids = _descendants(tree)
        trees.append((session, deepcopy(tree)))
        document_count += len(ids)
        if document_count > browser_cdp_frames.FRAME_LIMIT:
            raise BrowserSessionError(PAGE_CHECK_FAILED)
        for frame_id in ids:
            owner = protocol_int((await session.call("DOM.getFrameOwner", frameId=frame_id)).get("backendNodeId"))
            if not owner:
                raise BrowserSessionError(PAGE_CHECK_FAILED)
            viewport = await _owner_viewport(session, owner)
            frames.append(ChildFrame(session, frame_id, session, owner, viewport, session is page, False))
        return set()

    async def record_owner(session: Session, owner: int) -> bool:
        nonlocal document_count
        document_count += 1
        key = (session.session_id, owner)
        if document_count > browser_cdp_frames.FRAME_LIMIT or key in owners:
            raise BrowserSessionError(PAGE_CHECK_FAILED)
        owners[key] = await _owner_viewport(session, owner)
        # This walk needs every child, including descendants of a hidden frame.
        return True

    _, documents = await browser_cdp_frames.search_page(
        page, collect, record_owner, child_limit=lambda: browser_cdp_frames.FRAME_LIMIT - document_count)
    if len(documents) - 1 != len(owners):
        raise BrowserSessionError(PAGE_CHECK_FAILED)
    for key, (child, _shown) in documents.items():
        if key is None:
            continue
        parent_key, owner = browser_cdp_frames.document_owner(key)
        parent = documents[parent_key][0]
        viewport = owners[(parent.session_id, owner)]
        frames.append(ChildFrame(child, None, parent, owner, viewport, parent is page, True))
    processed = {frame.session.session_id for frame in frames if frame.cross_process}
    for target in page.attached:
        info = target.get("targetInfo")
        if not isinstance(info, dict) or not isinstance(info.get("type"), str):
            raise BrowserSessionError(PAGE_CHECK_FAILED)
        if info["type"] == "iframe" and target.get("sessionId") not in processed:
            raise BrowserSessionError(PAGE_CHECK_FAILED)
    return FrameWalk(frames, len(page.attached), trees)


async def moved(walk: FrameWalk) -> FrameWalk:
    """The same documents at their places now, after the page scrolled.

    The walk itself is kept: a cross-site frame attaches once per connection, so a second walk would not see
    it again. A frame that came or went since is caught by the walk's own stability check.
    """
    try:
        frames = [replace(frame, viewport=await _owner_viewport(frame.parent, frame.owner)) for frame in walk.frames]
    except (OSError, TimeoutError, WebSocketException, AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise BrowserSessionError(PAGE_CHECK_FAILED) from exc
    return replace(walk, frames=frames)


async def locate_in_frames(run: RunCommand, page: CdpClient, selector: str, box: dict | None,
                           walk: FrameWalk, *, defer_when_missed: bool = False) -> tuple[Located | None, str | None, str | None]:
    """Find a frame's unique holder, refusing ambiguity, unreadable documents and a moving frame."""
    try:
        return await _locate_in_frames(run, page, selector, box, walk, defer_when_missed=defer_when_missed)
    except (OSError, TimeoutError, WebSocketException, AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise BrowserSessionError(PAGE_CHECK_FAILED) from exc


async def _locate_in_frames(run: RunCommand, page: CdpClient, selector: str, box: dict | None,
                            walk: FrameWalk, *, defer_when_missed: bool) -> tuple[Located | None, str | None, str | None]:
    frames = walk.frames
    if not frames:
        if not await _stable_walk(page, walk):
            raise BrowserSessionError(PAGE_CHECK_FAILED)
        return None, None, None
    if box is None or is_empty(box):
        return None, None, None
    view = await page_viewport(page)
    if not all(_number(view.get(key)) for key in ("clientWidth", "clientHeight", "pageX", "pageY")):
        raise BrowserSessionError(PAGE_CHECK_FAILED)
    hits = []
    for frame in frames:
        viewport = frame.viewport
        shown = frame.direct and viewport is not None and _shows(view, viewport, box)
        located, cover = await _hit_in_frame(page, frame, box) if shown else (None, None)
        hits.append((frame, located, cover, shown))
    # No frame was hit: let the caller scroll before the one full document scan.
    if defer_when_missed and not any(located is not None for _, located, _, _ in hits):
        return None, "retry", None
    held = []
    for frame, located, cover, shown in hits:
        contains = await _holds(frame.session, frame.frame_id, box)
        if located is not None or contains:
            held.append((located, cover, shown))
    page_holds = in_viewport(view, box) and await _holds(page, None, box)
    stable_box = await read_box(run, selector) == box
    stable_frames = all([
        _place(view, frame.viewport, box) == _place(view, await _owner_viewport(frame.parent, frame.owner), box)
        for frame in frames
    ])
    stable_trees = await _stable_walk(page, walk)
    if not (stable_box and stable_frames and stable_trees):
        raise BrowserSessionError(PAGE_CHECK_FAILED)
    if len(held) + page_holds > 1:
        return None, "ambiguous", None
    if not held:
        return None, None, None
    located, cover, shown = held[0]
    if located is not None:
        return located, None, None
    return (None, "covered", cover) if shown else (None, None, None)


async def _stable_walk(page: CdpClient, walk: FrameWalk) -> bool:
    trees = all([(await session.call("Page.getFrameTree")).get("frameTree") == tree for session, tree in walk.trees])
    return trees and len(page.attached) == walk.attachment_count


def _shows(view: dict, viewport: Rect, box: dict) -> bool:
    placed = {**box, "x": box["x"] + viewport["x"], "y": box["y"] + viewport["y"]}
    return _inside(viewport, placed) and in_viewport(view, placed)


def _place(view: dict, viewport: Rect | None, box: dict) -> object:
    if viewport is None:
        return None
    return viewport if _shows(view, viewport, box) else (viewport["width"], viewport["height"])


async def _holds(cdp: Session, frame_id: str | None, box: dict) -> bool:
    result = await cdp.call(
        "Runtime.callFunctionOn", executionContextId=await isolated_context(cdp, frame_id), functionDeclaration=_HOLDS,
        arguments=[{"value": {key: box[key] for key in ("x", "y", "width", "height")}}, {"value": _HOLDS_LIMIT}],
        returnByValue=True, silent=True)
    value = result.get("result", {}).get("value")
    if "exceptionDetails" in result or not isinstance(value, bool):
        raise BrowserSessionError(PAGE_CHECK_FAILED)
    return value


async def _hit_in_frame(page: CdpClient, frame: ChildFrame, box: dict) -> tuple[Located | None, str | None]:
    viewport = frame.viewport
    if viewport is None:
        return None, None
    placed = {**box, "x": box["x"] + viewport["x"], "y": box["y"] + viewport["y"]}
    if not frame.cross_process:
        node = await node_for_box(page, placed, strict=True, max_ancestors=_HOLDS_LIMIT)
        if node:
            return Located(node, page, frame.frame_id), None
        return None, await cover_tag(page, placed, strict=True)
    if await node_at_centre(page, placed, strict=True) != frame.owner:
        return None, await cover_tag(page, placed, strict=True)
    node = await node_for_box(frame.session, box, strict=True, max_ancestors=_HOLDS_LIMIT)
    if node:
        return Located(node, frame.session, None, (viewport["x"], viewport["y"])), None
    return None, await cover_tag(frame.session, box, strict=True)


async def _owner_viewport(page: Session, owner: int) -> Rect | None:
    content = (await page.call("DOM.getBoxModel", backendNodeId=owner)).get("model", {}).get("content")
    if (not isinstance(content, list) or len(content) < 8
            or not all(_number(value) for value in content[:8])):
        raise BrowserSessionError(PAGE_CHECK_FAILED)
    viewport = {"x": content[0], "y": content[1], "width": content[2] - content[0], "height": content[5] - content[1]}
    return viewport if viewport["width"] > 0 and viewport["height"] > 0 else None


def _descendants(node: object) -> list[str]:
    pending = [(node, True)]
    found: list[str] = []
    while pending:
        current, root = pending.pop()
        if not isinstance(current, dict) or not isinstance(current.get("frame"), dict):
            raise BrowserSessionError(PAGE_CHECK_FAILED)
        frame_id = current["frame"].get("id")
        loader = current["frame"].get("loaderId")
        if not isinstance(frame_id, str) or not frame_id or not isinstance(loader, str) or not loader:
            raise BrowserSessionError(PAGE_CHECK_FAILED)
        if not root:
            found.append(frame_id)
            if len(found) >= browser_cdp_frames.FRAME_LIMIT:
                raise BrowserSessionError(PAGE_CHECK_FAILED)
        children = current.get("childFrames", [])
        if not isinstance(children, list) or len(children) + len(found) + len(pending) >= browser_cdp_frames.FRAME_LIMIT:
            raise BrowserSessionError(PAGE_CHECK_FAILED)
        pending.extend((child, False) for child in reversed(children))
    return found


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _inside(viewport: Rect, box: dict) -> bool:
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    return viewport["x"] <= x < viewport["x"] + viewport["width"] and viewport["y"] <= y < viewport["y"] + viewport["height"]


def shifted(rect: Rect, shift: tuple[float, float]) -> Rect:
    return {**rect, "x": rect["x"] + shift[0], "y": rect["y"] + shift[1]}
