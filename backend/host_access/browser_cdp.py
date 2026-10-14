"""CDP operations used by the single owned browser-session command path."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence
from urllib.parse import urlparse

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
        current = {_int(node) for node in nodes.get("nodeIds", [])}
        visible = {node for node in current | set(known_nodes) if await _visible(cdp, node)}
        return PasswordProbe(loader, host, frozenset(visible))


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
    endpoint = data.get("result") if isinstance(data, dict) else None
    if result.returncode or not isinstance(endpoint, str) or not endpoint.startswith("ws"):
        raise BrowserSessionError("This page couldn't be checked")
    return endpoint


async def _box(run: RunCommand, selector: str) -> dict | None:
    result = await run(("get", "box", selector), 10)
    payload = browser_cli.json_object(result.stdout)
    data = payload.get("data")
    box = data.get("result") if isinstance(data, dict) else None
    if not isinstance(box, dict) or not all(isinstance(box.get(key), (int, float)) for key in ("x", "y", "width", "height")):
        return None
    return box


async def _node_for_box(cdp: CdpClient, box: dict) -> int | None:
    location = await cdp.call("DOM.getNodeForLocation", x=box["x"] + box["width"] / 2,
                              y=box["y"] + box["height"] / 2, includeUserAgentShadowDOM=False,
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
