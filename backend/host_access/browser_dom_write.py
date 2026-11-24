"""Agent targets located in the shared page and page-context DOM writes while a person has the pane open (AR1)."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Callable, Sequence

from host_access import browser_cli
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
    async with CdpClient(endpoint) as cdp:
        try:
            backend_id, unresolved = await _locate(run, cdp, args[1])
            if backend_id is None:
                cover = await _cover(cdp, await _box(run, args[1])) if unresolved == "covered" else None
                raise DomWriteRefused(unresolved, cover)
            context_id = await _isolated_context(cdp)
            resolved = await cdp.call("DOM.resolveNode", backendNodeId=backend_id, executionContextId=context_id)
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
            await _report_field(cdp, backend_id, object_id, on_field)
    return "ok"


async def target_node(run: RunCommand, endpoint: str, selector: str) -> int | None:
    """The backendNodeId an agent ref or CSS selector names, located as the write path locates it.

    A read never scrolls the person's page, so a target outside the viewport is not located (None).
    """
    async with CdpClient(endpoint) as cdp:
        return (await _locate(run, cdp, selector, scroll=False))[0]


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


async def _locate(run: RunCommand, cdp: CdpClient, selector: str, *, scroll: bool = True) -> tuple[int | None, str]:
    """Find the node at the centre of agent-browser's box for a ref or selector, and why not if absent."""
    box = await _box(run, selector)
    if box is not None and not _is_empty(box) and not _in_viewport(await _viewport(cdp), box):
        if not scroll:
            return None, "no_box"
        # The hit test reads only the viewport. Scrolling moves neither focus nor pointer, and
        # agent-browser's own click scrolls its target into view the same way.
        await run(("scrollintoview", selector), 10)
        box = await _box(run, selector)
    node, reason = await _settled_hit(run, cdp, selector, box)
    if node is None and reason == "covered" and scroll:
        # A sticky header or a bottom bar covers part of the viewport, so a target in view can sit under
        # it. scrollintoview centres the target, as a person scrolls a link clear of a banner to click it.
        await run(("scrollintoview", selector), 10)
        node, reason = await _settled_hit(run, cdp, selector, await _box(run, selector))
    return node, reason


async def _settled_hit(run: RunCommand, cdp: CdpClient, selector: str, box: dict | None) -> tuple[int | None, str]:
    # The box comes from agent-browser and the hit test from CDP, so the page can move between the two
    # reads. A hit test holds only when the box was in view and the same before and after it.
    seen = False
    for _ in range(_SETTLE_READS):
        if box is None or _is_empty(box):
            # agent-browser reports an all-zero box, not an error, for a hidden element and for a ref
            # whose node the page replaced since the snapshot; for a ref the second is far likelier.
            return None, "stale_ref" if box is not None and _is_ref(selector) else "no_box"
        in_view = _in_viewport(await _viewport(cdp), box)
        seen = seen or in_view
        node = await _hit(cdp, selector, box) if in_view else None
        after = await _box(run, selector)
        if in_view and after == box:
            return node, "covered"
        box = after
        await asyncio.sleep(_SETTLE_INTERVAL)
    # The target never held still in view, so no hit test on it can be trusted.
    return None, "moving" if seen else "off_screen"


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


async def _report_field(cdp: CdpClient, node: int, object_id: str, on_field: FieldTarget) -> None:
    field = await border_rect(cdp, backendNodeId=node)
    if field is None or field["width"] <= 0 or field["height"] <= 0:
        # The write removed or hid its own field, so there is no field left to mark.
        return
    label = await cdp.call("Runtime.callFunctionOn", objectId=object_id, returnByValue=False, silent=True,
                           functionDeclaration="function() { return (this.labels && this.labels[0]) || null; }")
    label_id = label.get("result", {}).get("objectId")
    on_field(field, await border_rect(cdp, objectId=label_id) if isinstance(label_id, str) else None)


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
        # Hit-test as a real click does: a layer with pointer-events: none (a toast or overlay container
        # over the whole page) lets the click through, so it does not cover what lies beneath it.
        location = await cdp.call("DOM.getNodeForLocation", x=round(scroll_x + box["x"] + box["width"] / 2),
                                  y=round(scroll_y + box["y"] + box["height"] / 2), includeUserAgentShadowDOM=False,
                                  ignorePointerEventsNone=False)
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


async def _isolated_context(cdp: CdpClient) -> int:
    tree = await cdp.call("Page.getFrameTree")
    frame = tree.get("frameTree", {}).get("frame", {}).get("id")
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
