"""Agent targets located in the shared page and page-context DOM writes while a person has the pane open (AR1)."""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import AsyncExitStack
from typing import Callable, Sequence

from host_access import browser_dom_hit
from host_access.browser_cdp import CdpClient, Rect, RunCommand, border_rect, protocol_int
from host_access.browser_dom_frames import FrameTargets, locate_in_frames, shifted
from host_access.browser_dom_hit import (
    Located, box_matches as _box_matches, cover_tag as _cover, in_viewport as _in_viewport,
    is_empty as _is_empty, isolated_context as _isolated_context, node_at_centre as _node_at_centre,
    node_for_box as _node_for_box,
    remote_object_id as _object_id, page_viewport as _viewport, read_box as _box,
)
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
    "ambiguous": "an element at the same place in more than one frame on the page matches it",
    "moving": "the page kept moving",
    "off_screen": "it never came into view",
    "page_error": "the page refused the change",
    "rich_text": "it is a rich-text editor, which this path can't fill",
    "not_field": "it isn't a text field or a dropdown",
    "rejected_value": "the field didn't keep the value",
    "no_option": "the dropdown has no option with that value or label",
}
_SETTLE_INTERVAL = browser_dom_hit.SETTLE_INTERVAL
_SETTLE_READS = browser_dom_hit.SETTLE_READS


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
            found, unresolved, cover = await _locate(run, page, endpoint, args[1], FrameTargets(frame_clients))
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


async def is_dropdown(run: RunCommand, endpoint: str, selector: str) -> bool:
    """Whether an agent's ref or CSS selector names a native <select>, read through the DOM domain only.

    It runs no page script and reads no value. A target it can't hit-test (no box, covered, or still off
    screen after a scroll) is not reported as a dropdown, and neither is one it couldn't check.
    """
    box = await _box(run, selector)
    if box is None or _is_empty(box):
        return False
    async with CdpClient(endpoint) as cdp:
        try:
            if not _in_viewport(await _viewport(cdp), box):
                # With no pane open nobody sees the scroll, and the native fill scrolls its target the same way.
                await run(("scrollintoview", selector), 10)
                box = await _box(run, selector)
                if box is None or _is_empty(box):
                    return False
            node = await _node_at_centre(cdp, box)
            if not node or not await _box_matches(cdp, node, box):
                return False
            name = (await cdp.call("DOM.describeNode", backendNodeId=node)).get("node", {}).get("nodeName")
        except BrowserSessionError:
            return False
    return name == "SELECT"


async def _locate(run: RunCommand, page: CdpClient, endpoint: str, selector: str,
                  frame_targets: FrameTargets) -> tuple[Located | None, str, str | None]:
    """Find the node a write acts on, why not if absent, and the tag of what covers it.

    The main page is tried first. Only when it fails is the target looked for in a frame, because
    agent-browser measures an element inside a frame from that frame's own corner. Frames are searched
    before the page is scrolled to clear a cover, since a target in a frame always misses the page's hit
    test and would otherwise scroll the person's page on every write into a frame.
    """
    node, reason, box = await _locate_in_page(run, page, selector)
    for retry in (False, True):
        if node is not None:
            return Located(node, page), reason, None
        found, framed, framed_cover = await locate_in_frames(run, page, endpoint, selector, box, frame_targets)
        if found is not None:
            return found, "covered", None
        if framed == "ambiguous":
            return None, framed, None
        if reason != "covered":
            return None, reason, None
        if retry:
            break
        # A sticky header or a bottom bar covers part of the viewport, so a target in view can sit under
        # it. scrollintoview centres the target, as a person scrolls a link clear of a banner to click it.
        await run(("scrollintoview", selector), 10)
        node, reason, box = await _settled_hit(run, page, selector, await _box(run, selector))
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
    return await _settled_hit(run, cdp, selector, box)


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


def _is_ref(selector: str) -> bool:
    return re.fullmatch(r"(?:@|ref=)?e\d+", selector) is not None


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
    on_field(shifted(field, shift), shifted(label_box, shift) if label_box is not None else None)


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
