"""Browser-pane control authority, owned by the holding stream connection."""
from __future__ import annotations

from urllib.parse import urlparse

from infra import state
from host_access.browser_session_state import BrowserSessionError

USER_HAS_CONTROL = (
    "the user has taken control of the browser in the CORSIA pane. Stop browser work, "
    "tell the user you are waiting for the browser, and continue when they reply."
)

_control_holders: dict[str, str] = {}


def take_control(thread_id: str, connection_token: str) -> bool:
    """Claim a thread only when no other pane stream is its control holder."""
    holder = _control_holders.get(thread_id)
    if holder not in {None, connection_token}:
        return False
    _control_holders[thread_id] = connection_token
    return True


def hand_back(thread_id: str, connection_token: str) -> bool:
    """Release only the control held by this exact pane stream connection."""
    if _control_holders.get(thread_id) != connection_token:
        return False
    del _control_holders[thread_id]
    return True


def holds_control(thread_id: str, connection_token: str) -> bool:
    return _control_holders.get(thread_id) == connection_token


def require_page_control(thread_id: str, connection_token: str | None = None) -> None:
    """The one page-command rule: the holding stream acts; everyone else waits.

    The agent and a pane without a stream pass no token, so they act only while
    no pane holds control.
    """
    holder = _control_holders.get(thread_id)
    if holder is not None and holder != connection_token:
        raise BrowserSessionError(USER_HAS_CONTROL)


def is_http_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def announce_browser_opened(thread_id: str) -> None:
    """Tell the thread's pages a successful browser open is now streamable."""
    state.broadcast_to_thread(thread_id, {"type": "browser_opened", "thread_id": thread_id})
