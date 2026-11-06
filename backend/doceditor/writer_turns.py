"""Who holds a comment thread's writer turn right now.

A thread (its root comment) has one writer turn. A person holds it while a page of theirs is on the
thread: the card selected or its reply box in use (the browser's ``comment_turn`` frame, renewed at
most once a minute). An agent holds it only inside one write call (``agent_comment_writes``).
Turns are kept in memory only, so a restart releases them all (R5 of the hold-while-engaged design);
the agent writes waiting for a turn are durable in ``writer_line``.

Person holds change on the event loop and are read by the line on database workers. A read that
races an arrival is the same as the arrival landing a moment later: an agent woken just before the
person came back is put back in line by its own write, never let through. Agent holds are taken
inside the line's admission write, which ``BEGIN IMMEDIATE`` serializes with every other line write.
"""

from __future__ import annotations

from dataclasses import dataclass

#: A page that stops renewing loses the turn after this long; it renews every 60 s while on a thread.
LEASE_SECONDS = 90


@dataclass(frozen=True)
class PersonHold:
    user_id: str
    name: str
    artifact_id: str


_people: dict[str, dict[object, PersonHold]] = {}
_page_root: dict[object, str] = {}
_agents: dict[str, str] = {}


def person_holding(root_id: str) -> PersonHold | None:
    """The person whose page is on this thread, if any."""
    pages = _people.get(root_id)
    return next(iter(pages.values()), None) if pages else None


def agent_writing(root_id: str) -> str | None:
    """The agent whose write call is landing on this thread right now."""
    return _agents.get(root_id)


def is_free(root_id: str) -> bool:
    return person_holding(root_id) is None and agent_writing(root_id) is None


def take(page: object, root_id: str, hold: PersonHold) -> list[str]:
    """``page`` is now on ``root_id``; returns the thread it left, if that freed it."""
    freed = release(page) if _page_root.get(page) not in (None, root_id) else []
    _people.setdefault(root_id, {})[page] = hold
    _page_root[page] = root_id
    return freed


def release(page: object) -> list[str]:
    """``page`` left its thread (another thread, panel closed, sent, hidden, idle, gone).

    Returns the thread when no one holds it any more, so its line can be served.
    """
    root_id = _page_root.pop(page, None)
    if root_id is None:
        return []
    pages = _people.get(root_id, {})
    pages.pop(page, None)
    if pages:
        return []
    _people.pop(root_id, None)
    return [root_id] if root_id not in _agents else []


def page_root(page: object) -> str | None:
    return _page_root.get(page)


def people_on(root_id: str) -> set[str]:
    """The users with a page on this thread."""
    return {hold.user_id for hold in _people.get(root_id, {}).values()}


def agent_enter(root_id: str, thread_id: str) -> None:
    _agents[root_id] = thread_id


def agent_exit(root_id: str, thread_id: str) -> None:
    if _agents.get(root_id) == thread_id:
        _agents.pop(root_id, None)


def reset() -> None:
    """Forget every turn, as a restart does (tests)."""
    _people.clear()
    _page_root.clear()
    _agents.clear()
