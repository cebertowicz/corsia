"""Browser-pane operations on the composed browser session owner."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

from host_access import browser_cdp, browser_cli, browser_control, browser_pane_state, browser_screencast
from host_access.processes import PROCESS_OWNER

__all__ = ["HISTORY_ACTIONS", "PaneAppearance", "clear_page_endpoint", "go", "is_open", "key_event", "match_pane", "mouse_event", "open_url", "page_info", "password_probe", "screencast", "session_identity", "watch_page_metadata"]

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


async def watch_targets(thread_id: str, notify: Callable[[], None], known: list[set[str] | None]) -> None:
    """Observe tab lifecycle while a pane is connected.

    Only page targets count: frames and workers (a captcha's, say) come and go constantly, and
    each notice restarts the pane's streams. Chrome replays existing targets before it answers
    discovery; those seed the known pages without a notice. `known` outlives one connection, so
    a reconnect whose replay differs from the pages last seen notifies once: a tab opened or
    closed while the watch was down.
    """
    from host_access.browser_session_commands import browser_endpoint
    endpoint = await browser_endpoint(PROCESS_OWNER.browser_sessions(), thread_id)
    async with browser_cdp.CdpClient(endpoint) as cdp:
        discovery = await cdp.send("Target.setDiscoverTargets", discover=True)
        discovering = False
        seen = known[0]
        pages: set[str] = set()
        while True:
            event = json.loads(await cdp.socket.recv())
            if event.get("id") == discovery:
                discovering = True
                if seen is not None and pages != seen:
                    notify()
                known[0] = pages
                continue
            method = event.get("method")
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            info = params.get("targetInfo") if isinstance(params.get("targetInfo"), dict) else {}
            if method == "Target.targetCreated" and info.get("type") == "page":
                pages.add(info.get("targetId"))
            elif method == "Target.targetDestroyed" and params.get("targetId") in pages:
                pages.discard(params.get("targetId"))
            else:
                continue
            if discovering:
                notify()


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
    browser_pane_state.screencast_started(thread_id, connection_token, cast)
    try:
        await cast.run()
    finally:
        browser_pane_state.screencast_ended(thread_id, connection_token, cast)


def session_identity(thread_id: str) -> str | None:
    """Read the current runtime identity alongside a pane's page observation."""
    return PROCESS_OWNER.browser_sessions().session_identity(thread_id)


async def password_probe(thread_id: str, connection_token: str,
                         known: browser_pane_state.KnownNodes) -> browser_cdp.PasswordProbe:
    """Probe the shared page for a password field over this pane's page endpoint."""
    endpoint = await _page_endpoint(thread_id, connection_token, browser_control.PAGE_CHECK_FAILED)
    return await browser_cdp.page_password_probe(endpoint, known)


async def _page_endpoint(thread_id: str, connection_token: str, error: str) -> str:
    """This pane's page target, from the same resolver as the agent's commands, kept for the connection."""
    if (cached := _page_endpoints.get((thread_id, connection_token))) is not None:
        return cached
    owner = PROCESS_OWNER.browser_sessions()
    try:
        # A pane that leaves cancels its relay mid-read; cancelling a running command resets the shared session.
        page_endpoint = await asyncio.shield(owner.page_endpoint(thread_id))
    except browser_cdp.BrowserSessionError as exc:
        raise browser_cdp.BrowserSessionError(error) from exc
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
    """Open a pane URL: a connected pane navigates the open browser; without one, only a new session starts."""
    return await PROCESS_OWNER.browser_sessions().run(
        thread_id, ("open", url), session="new" if connection_token is None else "existing", pane=True,
        show_window=True,
    )


async def page_info(thread_id: str) -> tuple[str | None, str | None]:
    """Return the page URL and title for a pane label, without retaining either server-side."""
    owner = PROCESS_OWNER.browser_sessions()
    url = await owner.run(thread_id, ("get", "url"), session="existing", pane=True)
    title = await owner.run(thread_id, ("get", "title"), session="existing", pane=True)
    return _rendered_field(url, "url"), _rendered_field(title, "title")


def _rendered_field(rendered: str, key: str) -> str | None:
    """Read one value from owner.run's rendered result: agent-browser's `data` object, unwrapped."""
    if rendered.startswith("Error:"):
        return None
    value = browser_cli.json_object(rendered).get(key)
    return value if isinstance(value, str) else rendered


async def go(thread_id: str, action: str) -> str:
    """Go back, forward or reload in the open browser, for a connected pane."""
    if action not in HISTORY_ACTIONS:
        raise ValueError(f"unknown browser action: {action}")
    return await PROCESS_OWNER.browser_sessions().run(thread_id, (action,), session="existing", pane=True)


async def match_pane(thread_id: str, appearance: PaneAppearance) -> None:
    """Show the open browser's page at the pane's size, pixel density and CORSIA theme."""
    # Every pane on this thread shows the one shared page, so each captures at the new size from the
    # restart that follows the viewport command and sends the resized picture at once.
    # Size the JPEG to the pane's physical pixels; compact layouts use smaller JPEGs.
    browser_pane_state.capture_at(thread_id, round(appearance.width * appearance.scale),
                               round(appearance.height * appearance.scale), compact=appearance.width <= 480)
    owner = PROCESS_OWNER.browser_sessions()
    for args in (("set", "viewport", str(appearance.width), str(appearance.height), f"{appearance.scale:g}"),
                 ("set", "media", appearance.scheme)):
        await owner.run(thread_id, args, session="existing", pane=True)
