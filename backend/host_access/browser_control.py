"""Browser-pane control authority, owned by the holding stream connection."""
from __future__ import annotations

from infra import state
from host_access.browser_session_state import BrowserSessionError

USER_HAS_CONTROL = (
    "Error: the user has taken control of the browser in the CORSIA pane. "
    "Wait for them to hand it back; do not retry in a loop."
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


def has_user_control(thread_id: str) -> bool:
    return thread_id in _control_holders


def holds_control(thread_id: str, connection_token: str) -> bool:
    return _control_holders.get(thread_id) == connection_token


def refuse_agent_while_user_controls(thread_id: str) -> None:
    """Agent commands wait while a connected pane holds the page."""
    if has_user_control(thread_id):
        raise BrowserSessionError(USER_HAS_CONTROL)


def announce_browser_opened(thread_id: str) -> None:
    """Tell the thread's pages a successful browser open is now streamable."""
    state.broadcast_to_thread(thread_id, {"type": "browser_opened", "thread_id": thread_id})
