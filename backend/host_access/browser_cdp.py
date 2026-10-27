"""CDP operations used by the single owned browser-session command path."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlparse, urlunparse
from urllib.request import urlopen

import websockets

from host_access import browser_cli
from host_access.browser_domain import url_domain
from host_access.browser_session_state import BrowserSessionError

RunCommand = Callable[[Sequence[str], float], Awaitable[browser_cli.CommandResult]]
Rect = dict[str, float]
# The CDP plain-text search matches tag names, attribute names and values, and text, in every
# in-process document and in open and closed shadow roots; the probe keeps only password inputs.
_PASSWORD_SEARCH = "password"
_PASSWORD_AUTOCOMPLETE = {"current-password", "new-password"}


@dataclass(frozen=True)
class PasswordProbe:
    """The guarded page visit and visible/present password-node identities, never values or text.

    ``target`` is the page target probed, so a probe of another tab never settles the step open on this one.
    """

    visit: str
    host: str
    visible_nodes: frozenset[int]
    target: str = ""
    present_nodes: frozenset[int] = field(kw_only=True)


class CdpClient:
    """Small request/response CDP client; the session owner supplies its endpoint."""

    def __init__(self, endpoint: str, *, max_size: int | None = 2**20) -> None:
        self.endpoint = endpoint
        self._max_size = max_size
        self._next_id = 0

    def next_request(self) -> int:
        """The id of the next send, for a reader that must know its answer before send returns."""
        return self._next_id + 1

    async def send(self, method: str, **params: object) -> int:
        """Send one command without reading its response; the caller's reader receives it."""
        self._next_id += 1
        await self.socket.send(json.dumps({"id": self._next_id, "method": method, "params": params}))
        return self._next_id

    async def call(self, method: str, **params: object) -> dict:
        request_id = await self.send(method, **params)
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
        self.socket = await websockets.connect(self.endpoint, max_size=self._max_size, open_timeout=3, close_timeout=1)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.socket.close()


async def page_password_probe(endpoint: str, known: Mapping[str, frozenset[int]]) -> PasswordProbe:
    """Find visible password inputs on the page target, preserving this visit's reveal-toggle nodes (AR3).

    ``known`` maps each guarded visit to its password nodes; only those of the page's own visit apply.

    It covers the top document, in-process (same-origin) frames, and open and closed shadow roots,
    and transfers only the matching nodes, never the whole DOM.
    """
    async with CdpClient(endpoint) as cdp:
        frame = await cdp.call("Page.getFrameTree")
        loader = _loader_id(frame)
        host = _host(frame)
        # A node seen on an earlier visit can still resolve to a box after the page navigates away,
        # which would report a password field the new page does not have.
        known_here = known.get(loader, frozenset())
        current = await _password_inputs(cdp)
        visible = {node for node in current | known_here if await _visible(cdp, node)}
        return PasswordProbe(loader, host, frozenset(visible), endpoint, present_nodes=frozenset(current))


async def _password_inputs(cdp: CdpClient) -> set[int]:
    # Search results are pushed against this session's document, so it is requested first, without children.
    await cdp.call("DOM.getDocument", depth=0)
    search = await cdp.call("DOM.performSearch", query=_PASSWORD_SEARCH, includeUserAgentShadowDOM=False)
    search_id, count = search.get("searchId"), protocol_int(search.get("resultCount"))
    try:
        found = (await cdp.call("DOM.getSearchResults", searchId=search_id, fromIndex=0, toIndex=count)
                 if count else {})
    finally:
        await cdp.call("DOM.discardSearchResults", searchId=search_id)
    inputs = set()
    for node_id in found.get("nodeIds", []):
        node = (await cdp.call("DOM.describeNode", nodeId=protocol_int(node_id), depth=0)).get("node", {})
        if _is_password_input(node):
            inputs.add(protocol_int(node.get("backendNodeId")))
    return inputs


def _is_password_input(node: dict) -> bool:
    """`input[type=password]` or an input whose autocomplete names a password."""
    if str(node.get("nodeName", "")).upper() != "INPUT":
        return False
    flat = node.get("attributes") or []
    attributes = {str(name).lower(): str(value).strip().lower() for name, value in zip(flat[::2], flat[1::2])}
    return (attributes.get("type") == "password"
            or bool(_PASSWORD_AUTOCOMPLETE & set(attributes.get("autocomplete", "").split())))


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
        # CDP does not infer the held-button mask from `button`; a pointer press
        # must carry it or a background shared page can ignore the click sequence.
        params["buttons"] = 1 if event_type == "mousePressed" else 0
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
    await report({"type": "browser_metadata", "url": url, "domain": url_domain(url),
                  "title": title if isinstance(title, str) else None})


async def page_endpoint(run: RunCommand) -> str:
    """The one resolver of a session's shared page target, for agent commands, probes and the pane.

    It picks the page matching agent-browser's active URL and title. Pages with the same
    URL and title remain indistinguishable because the native CLI exposes no target ID.
    """
    browser_endpoint = _data_field(await run(("get", "cdp-url"), 10), "cdpUrl")
    if browser_endpoint is None or not browser_endpoint.startswith("ws"):
        raise BrowserSessionError("This page couldn't be checked")
    current_url = _data_field(await run(("get", "url"), 10), "url")
    current_title = _data_field(await run(("get", "title"), 10), "title")
    return await asyncio.to_thread(_page_endpoint, browser_endpoint, current_url, current_title)


def _data_field(result: browser_cli.CommandResult, key: str) -> str | None:
    data = browser_cli.json_object(result.stdout).get("data")
    value = data.get(key) if isinstance(data, dict) else None
    return value if not result.returncode and isinstance(value, str) else None


def _page_endpoint(browser_endpoint: str, expected_url: str | None = None, expected_title: str | None = None) -> str:
    """Resolve agent-browser's browser endpoint to the page its session currently displays."""
    parsed = urlparse(browser_endpoint)
    listing = urlunparse(("http", parsed.netloc, "/json/list", "", "", ""))
    try:
        with urlopen(listing, timeout=3) as response:  # nosec B310 - loopback CDP endpoint from agent-browser
            targets = json.load(response)
    except (OSError, ValueError) as exc:
        raise BrowserSessionError("This page couldn't be checked") from exc
    if not isinstance(targets, list):
        raise BrowserSessionError("This page couldn't be checked")
    pages = [target for target in targets if isinstance(target, dict) and target.get("type") == "page"]
    if expected_url:
        pages.sort(key=lambda target: (target.get("url") != expected_url,
                                       expected_title is not None and target.get("title") != expected_title))
    for target in pages:
        endpoint = target.get("webSocketDebuggerUrl")
        if isinstance(endpoint, str) and endpoint.startswith("ws"):
            return endpoint
    raise BrowserSessionError("This page couldn't be checked")


async def border_rect(cdp: CdpClient, **node: object) -> Rect | None:
    """A node's border box in the viewport's CSS pixels, or None when it renders no box."""
    try:
        model = await cdp.call("DOM.getBoxModel", **node)
    except BrowserSessionError:
        return None
    border = model.get("model", {}).get("border")
    if not isinstance(border, list) or len(border) < 8:
        return None
    x, y = border[0], border[1]
    return {"x": x, "y": y, "width": border[2] - x, "height": border[5] - y}


async def _visible(cdp: CdpClient, node: int) -> bool:
    rect = await border_rect(cdp, backendNodeId=node)
    return rect is not None and rect["width"] > 0 and rect["height"] > 0


def _loader_id(tree: dict) -> str:
    return str(tree.get("frameTree", {}).get("frame", {}).get("loaderId", ""))


def _host(tree: dict) -> str:
    url = tree.get("frameTree", {}).get("frame", {}).get("url")
    return urlparse(url).hostname or "" if isinstance(url, str) else ""


def protocol_int(value: object) -> int:
    if not isinstance(value, int):
        raise BrowserSessionError("This page couldn't be checked")
    return value
