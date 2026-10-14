"""Who drives a thread's browser: its agent, or the user from the CORSIA pane."""
from __future__ import annotations

from host_access.browser_session_state import BrowserSessionError

USER_HAS_CONTROL = (
    "Error: the user has taken control of the browser in the CORSIA pane. "
    "Wait for them to hand it back; do not retry in a loop."
)

_user_controlled: set[str] = set()


def set_user_control(thread_id: str, has_control: bool) -> None:
    if has_control:
        _user_controlled.add(thread_id)
    else:
        _user_controlled.discard(thread_id)


def has_user_control(thread_id: str) -> bool:
    return thread_id in _user_controlled


def refuse_agent_while_user_controls(thread_id: str) -> None:
    """Agent commands wait while the user holds the page; the pane's own commands do not."""
    if thread_id in _user_controlled:
        raise BrowserSessionError(USER_HAS_CONTROL)


def announce_browser_started(thread_id: str) -> None:
    """Tell the thread's open pages a browser exists, so the pane can add its tab."""
    from infra import state

    state.broadcast_to_thread(thread_id, {"type": "browser_started", "thread_id": thread_id})
