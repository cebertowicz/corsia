"""Agent targets located in the shared page and page-context DOM writes while a person has the pane open (AR1)."""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Callable, Sequence

from host_access import browser_cdp, browser_cli
from host_access.browser_cdp import CdpClient, Rect, RunCommand, border_rect, protocol_int
from host_access.browser_session_state import BrowserSessionError

# Where an agent wrote: the field's border box and its first label's, in page CSS pixels.
FieldTarget = Callable[[Rect, Rect | None], None]
# Agent commands that run as page-context DOM writes while a person has the pane open (AR1).
DOM_WRITES = frozenset({"click", "fill", "type", "select"})
_WRITE_REFUSALS = {
    "no_box": "the element has no box on the page",
    "stale_ref": ("the element has no box on the page; the page may have changed since your snapshot, "
                  "so take a new snapshot and use its refs"),
    "covered": "another element covers it",
    "moving": "the page kept moving",
    "off_screen": "it never came into view",
    "page_error": "the page refused the change",
    "rich_text": "it is a rich-text editor, which this path can't fill",
    "not_field": "it isn't a text field or a dropdown",
    "rejected_value": "the field didn't keep the value",
    "no_option": "the dropdown has no option with that value or label",
}
# A page that is still moving (a smooth scroll that scrollintoview started and did not wait for)
# is re-read at this interval, for at most this many reads, until a hit test holds. Each read also
# runs a box command and a hit test, so the wait is longer than reads times interval.
_SETTLE_INTERVAL = 0.05
_SETTLE_READS = 40


@dataclass(frozen=True)
class Located:
    """The node a write acts on, and where to reach it.

    ``cdp`` is the target the node lives in: the page, or a cross-site frame's own target. ``frame_id`` names
    the in-page frame whose world the write runs in (None for the target's main frame). ``shift`` is the
    frame origin to add to a rect measured by ``cdp`` for it to be in the page's pixels.
    """

    node: int
    cdp: CdpClient
    frame_id: str | None = None
    shift: tuple[float, float] = (0, 0)


@dataclass(frozen=True)
class ChildFrame:
    """A frame below the page: its id, its owner element in the page, where its viewport shows on the page.

    ``endpoint`` is the frame's own target when Chrome runs it in another process (a cross-site frame).
    """

    id: str
    owner: int
    viewport: Rect
    endpoint: str | None = None


class DomWriteRefused(BrowserSessionError):
    """A page-context write that could not be proven, refused instead of sent as the person's input."""

    def __init__(self, reason: str, cover: str | None = None) -> None:
        why = _WRITE_REFUSALS[reason] + (f" (a <{cover}>)" if cover else "")
        super().__init__(
            "The person has this browser open, so clicks and typing run inside the page without moving "
            f"their cursor, and this one couldn't: {why}. Nothing was sent as real "
            "input. Try another element, or ask the person in chat to do this step.")
        self.reason = reason


def is_dom_write(args: Sequence[str]) -> bool:
    """Whether an agent command is one the shared page runs as a page-context DOM write."""
    return len(args) >= 2 and args[0] in DOM_WRITES


async def shared_action(run: RunCommand, endpoint: str, args: Sequence[str],
                        on_field: FieldTarget | None = None) -> str:
    """Perform a click, fill, type or select inside the page without moving the shared focus or pointer.

    It returns "ok" only when the page function reports the write applied, and otherwise raises
    DomWriteRefused; it never falls back to agent-browser's native input, which moves the person's
    focus, caret and pointer. After a fill or type, ``on_field`` receives the written field's place.
    """
    async with CdpClient(endpoint) as page, AsyncExitStack() as frame_clients:
        try:
            found, unresolved, cover = await _locate(run, page, endpoint, args[1], frame_clients)
            if found is None:
                raise DomWriteRefused(unresolved, cover)
            cdp = found.cdp
            context_id = await _isolated_context(cdp, found.frame_id)
            resolved = await cdp.call("DOM.resolveNode", backendNodeId=found.node, executionContextId=context_id)
            object_id = _object_id(resolved)
            outcome = await cdp.call("Runtime.callFunctionOn", objectId=object_id, functionDeclaration=_WRITE,
                                     arguments=_write_arguments(args), awaitPromise=True, returnByValue=True,
                                     silent=True)
        except DomWriteRefused:
            raise
        except BrowserSessionError as exc:
            raise DomWriteRefused("page_error") from exc
        # A page function that throws is reported here, not as a protocol error.
        if "exceptionDetails" in outcome:
            raise DomWriteRefused("page_error")
        status = outcome.get("result", {}).get("value")
        if status != "ok":
            raise DomWriteRefused(status if status in _WRITE_REFUSALS else "page_error")
        if on_field is not None and args[0] in {"fill", "type"}:
            await _report_field(cdp, found.node, object_id, on_field, found.shift)
    return "ok"


async def target_node(run: RunCommand, endpoint: str, selector: str) -> int | None:
    """The backendNodeId an agent ref or CSS selector names, located as the write path locates it.

    A read never scrolls the person's page, so a target outside the viewport is not located (None). It
    reads the main page only: a node inside a frame is not located here, which the password guard treats
    as a target it cannot clear.
    """
    async with CdpClient(endpoint) as cdp:
        return (await _locate_in_page(run, cdp, selector, scroll=False))[0]


async def _locate(run: RunCommand, page: CdpClient, endpoint: str, selector: str,
                  frame_clients: AsyncExitStack) -> tuple[Located | None, str, str | None]:
    """Find the node a write acts on, why not if absent, and the tag of what covers it.

    The main page is tried first. Only when it fails is the target looked for in a frame, because
    agent-browser measures an element inside a frame from that frame's own corner.
    """
    node, reason, box = await _locate_in_page(run, page, selector)
    if node is not None:
        return Located(node, page), reason, None
    found, framed_cover, framed = await _locate_in_frames(run, page, endpoint, selector, box, frame_clients)
    if found is not None:
        return found, "covered", None
    if reason != "covered":
        return None, reason, None
    if framed:
        return None, reason, framed_cover
    return None, reason, await _cover(page, await _box(run, selector))


async def _locate_in_page(run: RunCommand, cdp: CdpClient, selector: str, *,
                          scroll: bool = True) -> tuple[int | None, str, dict | None]:
    """Find the node at the centre of agent-browser's box for a ref or selector, why not if absent, and the last box read."""
    box = await _box(run, selector)
    if box is not None and not _is_empty(box) and not _in_viewport(await _viewport(cdp), box):
        if not scroll:
            return None, "no_box", box
        # The hit test reads only the viewport. Scrolling moves neither focus nor pointer, and
        # agent-browser's own click scrolls its target into view the same way.
        await run(("scrollintoview", selector), 10)
        box = await _box(run, selector)
    node, reason, box = await _settled_hit(run, cdp, selector, box)
    if node is None and reason == "covered" and scroll:
        # A sticky header or a bottom bar covers part of the viewport, so a target in view can sit under
        # it. scrollintoview centres the target, as a person scrolls a link clear of a banner to click it.
        await run(("scrollintoview", selector), 10)
        node, reason, box = await _settled_hit(run, cdp, selector, await _box(run, selector))
    return node, reason, box


async def _locate_in_frames(run: RunCommand, page: CdpClient, endpoint: str, selector: str, box: dict | None,
                            frame_clients: AsyncExitStack) -> tuple[Located | None, str | None, bool]:
    """Find a target inside a child frame: the node, else the tag of what covers it, and whether a frame was tried.

    agent-browser measures an element inside a frame from that frame's own corner, so each frame's content-box
    origin moves the box onto the page. A frame counts only when the moved centre is inside both the page's
    viewport and the frame's own, and the page must hold still between the box and the hit test, as on the page.

    The search stops one level down: a frame nested inside a cross-site frame has its owner in that frame's
    process, not in this page, so it is never listed here and its elements are still refused.
    """
    cover: str | None = None
    for read in range(_SETTLE_READS):
        # The first box is the page path's last read; later ones are fresh, after the page moved.
        box = box if read == 0 else await _box(run, selector)
        if box is None or _is_empty(box):
            return None, None, False
        view = await _viewport(page)
        shown = [(frame, _on_page(box, frame)) for frame in await _child_frames(page, endpoint)]
        shown = [(frame, placed) for frame, placed in shown if _in_viewport(view, placed) and _inside(frame.viewport, placed)]
        if not shown:
            return None, None, False
        found, cover = None, None
        for frame, placed in shown:
            found, cover = await _hit_in_frame(page, frame, box, placed, frame_clients)
            if found is not None:
                break
        # The frame may have moved while it was hit, so the answer holds only if its place and the box did not.
        if await _box(run, selector) == box and [frame.viewport for frame, _ in shown] == [
                await _owner_viewport(page, frame.owner) for frame, _ in shown]:
            return found, cover, True
        await asyncio.sleep(_SETTLE_INTERVAL)
    return None, None, False


async def _hit_in_frame(page: CdpClient, frame: ChildFrame, box: dict, placed: dict,
                        frame_clients: AsyncExitStack) -> tuple[Located | None, str | None]:
    """The node a box inside a frame names, or the tag of what covers it."""
    if frame.endpoint is None:
        # A frame in the page's own process is hit-tested through the page, at the box moved onto the page.
        node = await _node_for_box(page, placed)
        if node:
            return Located(node, page, frame.id), None
        return None, await _cover(page, placed)
    # A cross-site frame shows on the page as its owner element; what is inside it is known only to its own target.
    if await _node_at_centre(page, placed) != frame.owner:
        return None, await _cover(page, placed)
    target = await frame_clients.enter_async_context(CdpClient(frame.endpoint))
    node = await _node_for_box(target, box)
    if node:
        return Located(node, target, None, (frame.viewport["x"], frame.viewport["y"])), None
    return None, await _cover(target, box)


async def _child_frames(page: CdpClient, endpoint: str) -> list[ChildFrame]:
    """Every frame whose owner element is in this page and shows a box, nearest the page first."""
    tree = await page.call("Page.getFrameTree")
    listed = [(frame_id, None) for frame_id in _descendants(tree.get("frameTree", {}))]
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


def _shifted(rect: Rect, shift: tuple[float, float]) -> Rect:
    return {**rect, "x": rect["x"] + shift[0], "y": rect["y"] + shift[1]}


async def _settled_hit(run: RunCommand, cdp: CdpClient, selector: str,
                       box: dict | None) -> tuple[int | None, str, dict | None]:
    # The box comes from agent-browser and the hit test from CDP, so the page can move between the two
    # reads. A hit test holds only when the box was in view and the same before and after it.
    seen = False
    for _ in range(_SETTLE_READS):
        if box is None or _is_empty(box):
            # agent-browser reports an all-zero box, not an error, for a hidden element and for a ref
            # whose node the page replaced since the snapshot; for a ref the second is far likelier.
            return None, "stale_ref" if box is not None and _is_ref(selector) else "no_box", box
        in_view = _in_viewport(await _viewport(cdp), box)
        seen = seen or in_view
        node = await _hit(cdp, selector, box) if in_view else None
        after = await _box(run, selector)
        if in_view and after == box:
            return node, "covered", box
        box = after
        await asyncio.sleep(_SETTLE_INTERVAL)
    # The target never held still in view, so no hit test on it can be trusted.
    return None, "moving" if seen else "off_screen", box


async def _hit(cdp: CdpClient, selector: str, box: dict) -> int | None:
    """The target node at the box's centre, or None when something else is there."""
    if not _is_ref(selector):
        found, node = await _css_target(cdp, selector, box)
        if found:
            return node
    return await _node_for_box(cdp, box)


async def _css_target(cdp: CdpClient, selector: str, box: dict) -> tuple[bool, int | None]:
    """Resolve the actual CSS target; a hit on it or its descendants is unobstructed."""
    context = await _isolated_context(cdp)
    result = await cdp.call("Runtime.evaluate", contextId=context, returnByValue=False, silent=True,
                            expression=f"document.querySelector({json.dumps(selector)})")
    target = result.get("result", {}).get("objectId")
    if "exceptionDetails" in result or not isinstance(target, str):
        # Native selectors in frames or its extended selector syntax (which querySelector throws on,
        # returning the error object's id) keep the existing resolver.
        return False, None
    description = await cdp.call("DOM.describeNode", objectId=target)
    node = protocol_int(description.get("node", {}).get("backendNodeId"))
    if not await _box_matches(cdp, node, box):
        # A same-named target in another selected frame is not this node.
        return False, None
    hit = await cdp.call(
        "Runtime.callFunctionOn", objectId=target, returnByValue=True, silent=True,
        functionDeclaration="""function(x, y) {
          const root = this.getRootNode();
          let hit = root.elementFromPoint(x, y);
          while (hit) {
            if (hit === this || this.contains(hit)) return true;
            const tree = hit.getRootNode();
            hit = tree instanceof ShadowRoot ? tree.host : null;
          }
          return false;
        }""", arguments=[{"value": box["x"] + box["width"] / 2},
                         {"value": box["y"] + box["height"] / 2}],
    )
    if hit.get("result", {}).get("value") is not True:
        return True, None
    return True, node


async def _viewport(cdp: CdpClient) -> dict:
    return (await cdp.call("Page.getLayoutMetrics")).get("cssVisualViewport", {})


def _is_ref(selector: str) -> bool:
    return re.fullmatch(r"(?:@|ref=)?e\d+", selector) is not None


def _is_empty(box: dict) -> bool:
    """The all-zero box agent-browser gives for an element with no layout; one zero side is a real element."""
    return not any(box[key] for key in ("x", "y", "width", "height"))


def _in_viewport(viewport: dict, box: dict) -> bool:
    width, height = viewport.get("clientWidth"), viewport.get("clientHeight")
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
        return False
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    return 0 <= x < width and 0 <= y < height


async def _report_field(cdp: CdpClient, node: int, object_id: str, on_field: FieldTarget,
                        shift: tuple[float, float] = (0, 0)) -> None:
    field = await border_rect(cdp, backendNodeId=node)
    if field is None or field["width"] <= 0 or field["height"] <= 0:
        # The write removed or hid its own field, so there is no field left to mark.
        return
    label = await cdp.call("Runtime.callFunctionOn", objectId=object_id, returnByValue=False, silent=True,
                           functionDeclaration="function() { return (this.labels && this.labels[0]) || null; }")
    label_id = label.get("result", {}).get("objectId")
    label_box = await border_rect(cdp, objectId=label_id) if isinstance(label_id, str) else None
    # A cross-site frame's own target measures from the frame's corner, so its rects move to the page's.
    on_field(_shifted(field, shift), _shifted(label_box, shift) if label_box is not None else None)


async def _box(run: RunCommand, selector: str) -> dict | None:
    result = await run(("get", "box", selector), 10)
    payload = browser_cli.json_object(result.stdout)
    data = payload.get("data")
    # agent-browser 0.31 exposes box geometry directly in data (older verified builds used data.result).
    box = data.get("result", data) if isinstance(data, dict) else None
    if not isinstance(box, dict) or not all(isinstance(box.get(key), (int, float)) for key in ("x", "y", "width", "height")):
        return None
    return box


async def _node_for_box(cdp: CdpClient, box: dict) -> int | None:
    node = await _node_at_centre(cdp, box)
    return await _matching_ancestor(cdp, node, box) if node else None


async def _node_at_centre(cdp: CdpClient, box: dict) -> int | None:
    # The box is in viewport pixels, but the hit test takes document pixels: add the scroll offset.
    viewport = await _viewport(cdp)
    scroll_x, scroll_y = viewport.get("pageX"), viewport.get("pageY")
    if not isinstance(scroll_x, (int, float)) or not isinstance(scroll_y, (int, float)):
        return None
    try:
        location = await cdp.call("DOM.getNodeForLocation", x=round(scroll_x + box["x"] + box["width"] / 2),
                                  y=round(scroll_y + box["y"] + box["height"] / 2), includeUserAgentShadowDOM=False,
                                  ignorePointerEventsNone=True)
    except BrowserSessionError:
        # Chrome reports no node at a point nothing hit-tests at.
        return None
    return protocol_int(location.get("backendNodeId"))


async def _cover(cdp: CdpClient, box: dict | None) -> str | None:
    """The tag of the element at the target's centre, so the agent can dismiss or scroll past it."""
    node = await _node_at_centre(cdp, box) if box is not None and not _is_empty(box) else None
    if not node:
        return None
    try:
        name = (await cdp.call("DOM.describeNode", backendNodeId=node)).get("node", {}).get("nodeName")
    except BrowserSessionError:
        return None
    # Only a tag name: page text, ids and classes stay out of the agent's error.
    return name.lower() if isinstance(name, str) and re.fullmatch(r"[A-Za-z][\w-]*", name) else None


async def _matching_ancestor(cdp: CdpClient, node: int, expected: dict) -> int | None:
    """Walk from the hit to a matching ancestor, including shadow hosts.

    Refs expose geometry but not identity; a same-sized overlay can still match.
    """
    context = None
    while node:
        if await _box_matches(cdp, node, expected):
            return node
        try:
            if context is None:
                context = await _isolated_context(cdp)
            resolved = await cdp.call("DOM.resolveNode", backendNodeId=node, executionContextId=context)
            parent = await cdp.call(
                "Runtime.callFunctionOn", objectId=_object_id(resolved), returnByValue=False, silent=True,
                functionDeclaration="function() { return this.parentElement || this.getRootNode().host || null; }",
            )
            object_id = parent.get("result", {}).get("objectId")
            if not isinstance(object_id, str):
                return None
            description = await cdp.call("DOM.describeNode", objectId=object_id)
            node = protocol_int(description.get("node", {}).get("backendNodeId"))
        except BrowserSessionError:
            # An inaccessible frame kept the old refusal when its hit box did not match.
            return None
    return None


async def _box_matches(cdp: CdpClient, node: int, expected: dict) -> bool:
    rect = await border_rect(cdp, backendNodeId=node)
    return rect is not None and all(abs(rect[key] - expected[key]) <= 1 for key in ("x", "y", "width", "height"))


async def _isolated_context(cdp: CdpClient, frame_id: str | None = None) -> int:
    """A world of ours in a frame: the target's main frame, or the in-page frame named."""
    if frame_id is None:
        tree = await cdp.call("Page.getFrameTree")
        frame = tree.get("frameTree", {}).get("frame", {}).get("id")
    else:
        frame = frame_id
    world = await cdp.call("Page.createIsolatedWorld", frameId=frame, worldName="corsia-shared-browser", grantUniveralAccess=False)
    return protocol_int(world.get("executionContextId"))


# The fixed page-context write (Governance R4): it takes only a command, values and an append flag,
# returns only "ok" or the reason it could not write, and never returns a field's value.
_WRITE = """function(command, values, append) {
  if (command === 'click') {
    // SVG and other non-HTML elements have no click(); a bubbling click event reaches the same handlers.
    if (typeof this.click === 'function') this.click();
    else this.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, composed: true}));
    return 'ok';
  }
  const changed = () => {
    this.dispatchEvent(new Event('input', {bubbles: true}));
    this.dispatchEvent(new Event('change', {bubbles: true}));
  };
  if (this instanceof HTMLSelectElement) {
    const options = Array.from(this.options);
    const chosen = values.map(value => options.find(option => option.value === value)
      || options.find(option => option.label === value));
    if (!chosen.length || chosen.some(option => !option)) return 'no_option';
    if (this.multiple) options.forEach(option => { option.selected = chosen.includes(option); });
    else Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(this, chosen[0].value);
    changed();
    return 'ok';
  }
  if (command === 'select') return 'not_field';
  const proto = this instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
    : this instanceof HTMLInputElement ? HTMLInputElement.prototype : null;
  if (proto === null) return this.isContentEditable ? 'rich_text' : 'not_field';
  const value = append ? this.value + values[0] : values[0];
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(this, value);
  changed();
  return this.value === value ? 'ok' : 'rejected_value';
}"""


def _write_arguments(args: Sequence[str]) -> list[dict]:
    values = list(args[2:]) if args[0] == "select" else [args[2] if len(args) > 2 else ""]
    return [{"value": args[0]}, {"value": values}, {"value": args[0] == "type"}]


def _object_id(result: dict) -> str:
    value = result.get("object")
    if not isinstance(value, dict) or not isinstance(value.get("objectId"), str):
        raise BrowserSessionError("This page couldn't be checked")
    return value["objectId"]
