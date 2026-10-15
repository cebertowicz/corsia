"""Browser-pane operations on the composed browser session owner."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from host_access import browser_cdp, browser_cli, browser_control, browser_screencast
from host_access.processes import PROCESS_OWNER

__all__ = ["HISTORY_ACTIONS", "PaneAppearance", "clear_page_endpoint", "go", "is_open", "key_event", "match_pane", "mouse_event", "open_url", "page_info", "password_probe", "screencast", "watch_page_metadata"]

HISTORY_ACTIONS = frozenset({"back", "forward", "reload"})
_page_endpoints: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class PaneAppearance:
    """The pane's size in CSS pixels, its pixel density and CORSIA's resolved theme."""

    width: int
    height: int
    scale: float
    scheme: str

    @classmethod
    def parse(cls, data: dict) -> PaneAppearance | None:
        """Accept only a screen-sized pane and a light or dark theme."""
        numbers = [data.get(key) for key in ("width", "height", "scale")]
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in numbers):
            return None
        width, height, scale = (float(value) for value in numbers if isinstance(value, (int, float)))
        if not (200 <= width <= 7680 and 150 <= height <= 4320 and 1 <= scale <= 4):
            return None
        if data.get("scheme") not in {"light", "dark"}:
            return None
        return cls(int(width), int(height), round(scale, 2), data["scheme"])


async def watch_page_metadata(thread_id: str, connection_token: str, report: Callable[[dict], Awaitable[None]]) -> None:
    """Forward CDP main-frame metadata changes for this connected shared pane."""
    await browser_cdp.watch_page_metadata(await _page_endpoint(thread_id, connection_token, "This page couldn't be checked"), report)


def clear_page_endpoint(thread_id: str, connection_token: str) -> None:
    _page_endpoints.pop((thread_id, connection_token), None)


async def is_open(thread_id: str) -> bool:
    return await PROCESS_OWNER.browser_sessions().has_session(thread_id)


async def screencast(thread_id: str, connection_token: str, deliver: Callable[[str], object]) -> None:
    """Show the shared page in this pane until it leaves, restarting after each page-metrics change."""
    endpoint = await _page_endpoint(thread_id, connection_token, "This page couldn't be shown")
    cast = browser_screencast.PaneScreencast(endpoint, deliver)
    browser_control.screencast_started(thread_id, connection_token, cast)
    try:
        await cast.run()
    finally:
        browser_control.screencast_ended(thread_id, connection_token, cast)


async def password_probe(thread_id: str, connection_token: str, known_visit: str | None,
                         known_nodes: frozenset[int]) -> browser_cdp.PasswordProbe:
    """Probe the shared page for a password field over this pane's page endpoint."""
    endpoint = await _page_endpoint(thread_id, connection_token, browser_control.PASSWORD_CHECK_FAILED)
    return await browser_cdp.page_password_probe(endpoint, known_visit, known_nodes)


async def _page_endpoint(thread_id: str, connection_token: str, error: str) -> str:
    if (cached := _page_endpoints.get((thread_id, connection_token))) is not None:
        return cached
    owner = PROCESS_OWNER.browser_sessions()
    # A pane that leaves cancels its relay mid-read; cancelling a running command resets the shared session.
    payload = await asyncio.shield(owner.run(thread_id, ("get", "cdp-url"), control_token=connection_token, session="existing"))
    # owner.run returns the rendered model result: agent-browser's `data` object, unwrapped.
    endpoint = browser_cli.json_object(payload).get("cdpUrl")
    if not isinstance(endpoint, str) or not endpoint.startswith("ws"):
        raise browser_cdp.BrowserSessionError(error)
    current_url = _rendered_field(
        await asyncio.shield(owner.run(thread_id, ("get", "url"), control_token=connection_token, session="existing")), "url")
    page_endpoint = await asyncio.to_thread(
        browser_cdp._page_endpoint, endpoint,
        current_url if current_url and current_url.startswith(("http://", "https://")) else None,
    )
    _page_endpoints[(thread_id, connection_token)] = page_endpoint
    return page_endpoint


async def mouse_event(thread_id: str, event: dict, connection_token: str) -> None:
    """Deliver mouse and wheel input in order with pane keyboard events."""
    endpoint = await _page_endpoint(thread_id, connection_token, "This page couldn't receive pointer input")
    await browser_cdp.dispatch_mouse_event(endpoint, event)


async def key_event(thread_id: str, event: dict, connection_token: str) -> None:
    """Deliver person keyboard input to the shared page with CDP modifier bits intact."""
    endpoint = await _page_endpoint(thread_id, connection_token, "This page couldn't receive keyboard input")
    await browser_cdp.dispatch_key_event(endpoint, event)


async def open_url(thread_id: str, url: str, connection_token: str | None = None) -> str:
    """Open a pane URL: the holding stream navigates the open browser; without one, only a new session starts."""
    return await PROCESS_OWNER.browser_sessions().run(
        thread_id, ("open", url), control_token=connection_token,
        session="new" if connection_token is None else "existing", pane=True,
    )


async def page_info(thread_id: str, connection_token: str) -> tuple[str | None, str | None]:
    """Return the page URL and title for a pane label, without retaining either server-side."""
    owner = PROCESS_OWNER.browser_sessions()
    url = await owner.run(thread_id, ("get", "url"), control_token=connection_token, session="existing")
    title = await owner.run(thread_id, ("get", "title"), control_token=connection_token, session="existing")
    return _rendered_field(url, "url"), _rendered_field(title, "title")


def _rendered_field(rendered: str, key: str) -> str | None:
    """Read one value from owner.run's rendered result: agent-browser's `data` object, unwrapped."""
    if rendered.startswith("Error:"):
        return None
    value = browser_cli.json_object(rendered).get(key)
    return value if isinstance(value, str) else rendered


async def go(thread_id: str, action: str, connection_token: str) -> str:
    """Go back, forward or reload in the open browser, for the holding stream."""
    if action not in HISTORY_ACTIONS:
        raise ValueError(f"unknown browser action: {action}")
    return await PROCESS_OWNER.browser_sessions().run(
        thread_id, (action,), control_token=connection_token, session="existing",
    )


async def match_pane(thread_id: str, appearance: PaneAppearance, connection_token: str) -> None:
    """Show the open browser's page at the pane's size, pixel density and CORSIA theme."""
    # Every pane on this thread shows the one shared page, so each captures at the new size from the
    # restart that follows the viewport command and sends the resized picture at once.
    for cast in browser_control.screencasts(thread_id):
        cast.capture_at(round(appearance.width * appearance.scale), round(appearance.height * appearance.scale))
    owner = PROCESS_OWNER.browser_sessions()
    for args in (("set", "viewport", str(appearance.width), str(appearance.height), f"{appearance.scale:g}"),
                 ("set", "media", appearance.scheme)):
        await owner.run(thread_id, args, control_token=connection_token, session="existing")
