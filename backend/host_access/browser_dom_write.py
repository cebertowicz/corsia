"""Agent targets located in the shared page and page-context DOM writes while a person has the pane open (AR1)."""
from __future__ import annotations

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
    "covered": "another element covers it",
    "page_error": "the page refused the change",
    "rich_text": "it is a rich-text editor, which this path can't fill",
    "not_field": "it isn't a text field or a dropdown",
    "rejected_value": "the field didn't keep the value",
    "no_option": "the dropdown has no option with that value or label",
}


class DomWriteRefused(BrowserSessionError):
    """A page-context write that could not be proven, refused instead of sent as the person's input."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            "The person has this browser open, so clicks and typing run inside the page without moving "
            f"their cursor, and this one couldn't: {_WRITE_REFUSALS[reason]}. Nothing was sent as real "
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
                raise DomWriteRefused(unresolved)
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


async def _locate(run: RunCommand, cdp: CdpClient, selector: str, *, scroll: bool = True) -> tuple[int | None, str]:
    """Find the node at the centre of agent-browser's box for a ref or selector, and why not if absent."""
    box = await _box(run, selector)
    if box is not None and not _in_viewport(await _viewport(cdp), box):
        if not scroll:
            return None, "no_box"
        # The hit test reads only the viewport. Scrolling moves neither focus nor pointer, and
        # agent-browser's own click scrolls its target into view the same way.
        await run(("scrollintoview", selector), 10)
        box = await _box(run, selector)
    if box is None:
        return None, "no_box"
    if not re.fullmatch(r"(?:@|ref=)?e\d+", selector):
        found, node = await _css_target(cdp, selector, box)
        if found:
            return node, "covered"
    node = await _node_for_box(cdp, box)
    return node, "covered"


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
    node = protocol_int(location.get("backendNodeId"))
    return await _matching_ancestor(cdp, node, box)


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
