"""CDP operations used by the single owned browser-session command path."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence
from urllib.parse import urlparse, urlunparse
from urllib.request import urlopen

import websockets

from host_access import browser_cli
from host_access.browser_session_state import BrowserSessionError

RunCommand = Callable[[Sequence[str], float], Awaitable[browser_cli.CommandResult]]


@dataclass(frozen=True)
class PasswordProbe:
    """The guarded page visit and visible password-node identities, never values."""

    visit: str
    host: str
    visible_nodes: frozenset[int]


class CdpClient:
    """Small request/response CDP client; the session owner supplies its endpoint."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._next_id = 0

    async def call(self, method: str, **params: object) -> dict:
        self._next_id += 1
        request_id = self._next_id
        await self.socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
        while True:
            response = json.loads(await self.socket.recv())
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise BrowserSessionError("This page couldn't be checked")
            result = response.get("result")
            if not isinstance(result, dict):
                raise BrowserSessionError("This page couldn't be checked")
            return result

    async def __aenter__(self) -> CdpClient:
        self.socket = await websockets.connect(self.endpoint, max_size=2**20, open_timeout=3, close_timeout=1)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.socket.close()


async def password_probe(run: RunCommand, known_nodes: frozenset[int]) -> PasswordProbe:
    """Find visible password controls in CDP DOM state, preserving reveal-toggle nodes."""
    endpoint = await _endpoint(run)
    async with CdpClient(endpoint) as cdp:
        frame = await cdp.call("Page.getFrameTree")
        loader = _loader_id(frame)
        host = _host(frame)
        document = await cdp.call("DOM.getDocument", depth=-1, pierce=True)
        root = _int(document.get("root", {}).get("nodeId"))
        nodes = await cdp.call("DOM.querySelectorAll", nodeId=root, selector="input[type=password],input[autocomplete=current-password],input[autocomplete=new-password]")
        current = {await _backend_node_id(cdp, _int(node)) for node in nodes.get("nodeIds", [])}
        visible = {node for node in current | set(known_nodes) if await _visible(cdp, node)}
        return PasswordProbe(loader, host, frozenset(visible))


async def watch_page_metadata(endpoint: str, report: Callable[[dict], Awaitable[None]]) -> None:
    """Report main-frame URL and title changes from CDP events until the pane closes."""
    async with CdpClient(endpoint) as cdp:
        await cdp.call("Page.enable")
        await _report_page_metadata(cdp, report)
        while True:
            event = json.loads(await cdp.socket.recv())
            method = event.get("method")
            params = event.get("params")
            if not isinstance(params, dict):
                continue
            if method == "Page.frameNavigated":
                frame = params.get("frame")
                if isinstance(frame, dict) and not frame.get("parentId"):
                    await _report_page_metadata(cdp, report, frame.get("url"))
            elif method in {"Page.navigatedWithinDocument", "Page.loadEventFired"}:
                await _report_page_metadata(cdp, report)


async def dispatch_mouse_event(endpoint: str, event: dict) -> None:
    """Send one pane pointer or wheel event through the same CDP target as keys."""
    event_type = event.get("eventType")
    x, y = event.get("x"), event.get("y")
    if (event_type not in {"mouseMoved", "mousePressed", "mouseReleased", "mouseWheel"}
            or not isinstance(x, (int, float)) or isinstance(x, bool)
            or not isinstance(y, (int, float)) or isinstance(y, bool)):
        raise BrowserSessionError("This page couldn't receive pointer input")
    params: dict[str, object] = {"type": event_type, "x": x, "y": y}
    if event_type == "mouseWheel":
        delta_x, delta_y = event.get("deltaX"), event.get("deltaY")
        if (not isinstance(delta_x, (int, float)) or isinstance(delta_x, bool)
                or not isinstance(delta_y, (int, float)) or isinstance(delta_y, bool)):
            raise BrowserSessionError("This page couldn't receive pointer input")
        params.update(deltaX=delta_x, deltaY=delta_y)
    else:
        button = event.get("button")
        click_count = event.get("clickCount")
        if button is not None:
            if button not in {"none", "left", "middle", "right", "back", "forward"}:
                raise BrowserSessionError("This page couldn't receive pointer input")
            params["button"] = button
        if click_count is not None:
            if not isinstance(click_count, int) or isinstance(click_count, bool) or click_count < 0:
                raise BrowserSessionError("This page couldn't receive pointer input")
            params["clickCount"] = click_count
    async with CdpClient(endpoint) as cdp:
        await cdp.call("Input.dispatchMouseEvent", **params)


async def dispatch_key_event(endpoint: str, event: dict) -> None:
    """Send a pane key event through CDP so Chrome receives its modifier state."""
    event_type = event.get("eventType")
    key = event.get("key")
    code = event.get("code")
    key_code = event.get("keyCode")
    modifiers = event.get("modifiers", 0)
    if (event_type not in {"keyDown", "keyUp"} or not isinstance(key, str)
            or not isinstance(code, str) or not isinstance(key_code, int) or not isinstance(modifiers, int)):
        raise BrowserSessionError("This page couldn't receive keyboard input")
    text = event.get("text") if event_type == "keyDown" else None
    is_text = isinstance(text, str) and bool(text)
    params: dict[str, object] = {
        # Chrome applies modifier shortcuts only to a keyDown event; rawKeyDown carries
        # the bit but does not consistently update the page's shortcut state.
        "type": "keyDown" if event_type == "keyDown" else event_type,
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": key_code,
        "modifiers": modifiers,
    }
    if is_text:
        params["text"] = text
    # DevTools does not infer editing commands from synthetic Meta/Ctrl shortcuts.
    # Preserve the DOM key event and ask Chrome to perform the matching native edit.
    if event_type == "keyDown" and key.lower() == "a" and modifiers & 6:
        params["commands"] = ["selectAll"]
    async with CdpClient(endpoint) as cdp:
        await cdp.call("Input.dispatchKeyEvent", **params)


async def _report_page_metadata(cdp: CdpClient, report: Callable[[dict], Awaitable[None]], url: object = None) -> None:
    """Read page metadata in direct response to a CDP navigation event, never by polling."""
    if not isinstance(url, str):
        tree = await cdp.call("Page.getFrameTree")
        url = tree.get("frameTree", {}).get("frame", {}).get("url")
    if not isinstance(url, str):
        return
    try:
        title_result = await cdp.call("Runtime.evaluate", expression="document.title", returnByValue=True, silent=True)
        title = title_result.get("result", {}).get("value")
    except BrowserSessionError:
        title = None
    await report({"type": "browser_metadata", "url": url, "title": title if isinstance(title, str) else None})


async def shared_action(run: RunCommand, args: Sequence[str]) -> str | None:
    """Perform a ref-resolved fill/type/click without moving the shared focus or pointer."""
    if len(args) < 2 or args[0] not in {"click", "fill", "type"}:
        return None
    selector = args[1]
    box = await _box(run, selector)
    if box is None:
        return None
    endpoint = await _endpoint(run)
    async with CdpClient(endpoint) as cdp:
        backend_id = await _node_for_box(cdp, box)
        if backend_id is None:
            return None
        context_id = await _isolated_context(cdp)
        resolved = await cdp.call("DOM.resolveNode", backendNodeId=backend_id, executionContextId=context_id)
        object_id = _object_id(resolved)
        declaration, arguments = _action(args)
        await cdp.call("Runtime.callFunctionOn", objectId=object_id, functionDeclaration=declaration,
                       arguments=arguments, awaitPromise=True, returnByValue=True, silent=True)
    return "ok"


async def _endpoint(run: RunCommand) -> str:
    result = await run(("get", "cdp-url"), 10)
    payload = browser_cli.json_object(result.stdout)
    data = payload.get("data")
    endpoint = data.get("cdpUrl") if isinstance(data, dict) else None
    if result.returncode or not isinstance(endpoint, str) or not endpoint.startswith("ws"):
        raise BrowserSessionError("This page couldn't be checked")
    return await asyncio.to_thread(_page_endpoint, endpoint)


def _page_endpoint(browser_endpoint: str) -> str:
    """Resolve agent-browser's browser-level endpoint to its current page target."""
    parsed = urlparse(browser_endpoint)
    listing = urlunparse(("http", parsed.netloc, "/json/list", "", "", ""))
    try:
        with urlopen(listing, timeout=3) as response:  # nosec B310 - loopback CDP endpoint from agent-browser
            targets = json.load(response)
    except (OSError, ValueError) as exc:
        raise BrowserSessionError("This page couldn't be checked") from exc
    if not isinstance(targets, list):
        raise BrowserSessionError("This page couldn't be checked")
    for target in targets:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        endpoint = target.get("webSocketDebuggerUrl")
        if isinstance(endpoint, str) and endpoint.startswith("ws"):
            return endpoint
    raise BrowserSessionError("This page couldn't be checked")


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
    location = await cdp.call("DOM.getNodeForLocation", x=round(box["x"] + box["width"] / 2),
                              y=round(box["y"] + box["height"] / 2), includeUserAgentShadowDOM=False,
                              ignorePointerEventsNone=True)
    node = _int(location.get("backendNodeId"))
    return node if await _matching_box(cdp, node, box) else None


async def _matching_box(cdp: CdpClient, node: int, expected: dict) -> bool:
    description = await cdp.call("DOM.describeNode", backendNodeId=node, depth=20, pierce=True)
    current = description.get("node")
    while isinstance(current, dict):
        backend = _int(current.get("backendNodeId"))
        if await _box_matches(cdp, backend, expected):
            return True
        current = current.get("parent")
    return False


async def _box_matches(cdp: CdpClient, node: int, expected: dict) -> bool:
    try:
        model = await cdp.call("DOM.getBoxModel", backendNodeId=node)
    except BrowserSessionError:
        return False
    border = model.get("model", {}).get("border")
    if not isinstance(border, list) or len(border) < 8:
        return False
    x, y = border[0], border[1]
    width, height = border[2] - x, border[5] - y
    return all(abs(actual - expected[key]) <= 1 for actual, key in ((x, "x"), (y, "y"), (width, "width"), (height, "height")))


async def _backend_node_id(cdp: CdpClient, node_id: int) -> int:
    description = await cdp.call("DOM.describeNode", nodeId=node_id, depth=0, pierce=True)
    return _int(description.get("node", {}).get("backendNodeId"))


async def _visible(cdp: CdpClient, node: int) -> bool:
    try:
        model = await cdp.call("DOM.getBoxModel", backendNodeId=node)
    except BrowserSessionError:
        return False
    border = model.get("model", {}).get("border")
    return isinstance(border, list) and len(border) >= 8 and border[2] > border[0] and border[5] > border[1]


async def _isolated_context(cdp: CdpClient) -> int:
    tree = await cdp.call("Page.getFrameTree")
    frame = tree.get("frameTree", {}).get("frame", {}).get("id")
    world = await cdp.call("Page.createIsolatedWorld", frameId=frame, worldName="corsia-shared-browser", grantUniveralAccess=False)
    return _int(world.get("executionContextId"))


def _action(args: Sequence[str]) -> tuple[str, list[dict]]:
    if args[0] == "click":
        return "function() { this.click(); }", []
    text = args[2] if len(args) > 2 else ""
    return ("function(value, append) { const proto = this instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; const setter = Object.getOwnPropertyDescriptor(proto, 'value').set; setter.call(this, append ? this.value + value : value); this.dispatchEvent(new Event('input', {bubbles:true})); this.dispatchEvent(new Event('change', {bubbles:true})); }", [{"value": text}, {"value": args[0] == "type"}])


def _loader_id(tree: dict) -> str:
    return str(tree.get("frameTree", {}).get("frame", {}).get("loaderId", ""))


def _host(tree: dict) -> str:
    url = tree.get("frameTree", {}).get("frame", {}).get("url")
    return urlparse(url).hostname or "" if isinstance(url, str) else ""


def _object_id(result: dict) -> str:
    value = result.get("object")
    if not isinstance(value, dict) or not isinstance(value.get("objectId"), str):
        raise BrowserSessionError("This page couldn't be checked")
    return value["objectId"]


def _int(value: object) -> int:
    if not isinstance(value, int):
        raise BrowserSessionError("This page couldn't be checked")
    return value
