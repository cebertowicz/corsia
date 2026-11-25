"""The one reader of the agent's browser's page targets, shared by the page resolver and the capture's page watch."""
from __future__ import annotations

import json
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

from host_access.browser_session_state import BrowserSessionError

PAGES_UNREADABLE = "This page couldn't be checked"


def page_targets(endpoint: str, timeout: float = 3) -> list[dict]:
    """Every page target in the agent's browser, read from /json/list on the CDP port ``endpoint`` is on.

    Refused with :data:`PAGES_UNREADABLE` when the list can't be read or isn't a list, so no caller mistakes an
    unreadable browser for one with no pages.
    """
    listing = urlunsplit(("http", urlsplit(endpoint).netloc, "/json/list", "", ""))
    try:
        with urlopen(listing, timeout=timeout) as response:  # nosec B310 - loopback CDP endpoint from agent-browser
            targets = json.load(response)
    except (OSError, ValueError) as exc:
        raise BrowserSessionError(PAGES_UNREADABLE) from exc
    if not isinstance(targets, list):
        raise BrowserSessionError(PAGES_UNREADABLE)
    return [target for target in targets if isinstance(target, dict) and target.get("type") == "page"]
