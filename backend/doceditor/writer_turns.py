"""Who holds a comment thread's or a document section's writer turn right now.

A thread (its root comment) has one writer turn. A person holds it while a page of theirs is on the
thread: the card selected or its reply box in use (the browser's ``comment_turn`` frame, renewed at
most once a minute). An agent holds it only inside one write call (``agent_comment_writes``).

Each section of an open document (``doc_sections``) has one too, under its ``section_key``. A
person holds it while their edits land in it (``ws/section_turn``; the page is the socket and the
document), and an agent only inside one edit or suggestion call (``section_writes``).
Turns are kept in memory only, so a restart releases them all; the agent writes waiting for a turn
are durable in ``writer_line``.

Person holds change on the event loop; agent holds change, and both are read, on database workers.
One lock guards every read and change, so no reader sees a map mid-change. A person arriving while
an agent's write is landing still takes their hold: the agent's write finishes, and the next agent
write on the thread waits for the person. Agent holds are taken inside the line's admission write,
which ``BEGIN IMMEDIATE`` serializes with every other line write.
"""

from __future__ import annotations

import threading
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
_lock = threading.Lock()
#: Agents with a woken section row: a hint that their next write may answer it, and that they have
#: none to take a section turn past (``section_writes``); the line rows decide. Set when a row wakes,
#: cleared by ``writer_line.settle``, the one end of a row.
_section_wakes: set[str] = set()

_SECTION = "section:"


def section_key(artifact_id: str, heading: str) -> str:
    """The turn of a document's section, named as ``doc_sections`` names it; it shares the comment line's keys."""
    return f"{_SECTION}{artifact_id}:{heading}"


def section_heading(key: str) -> str | None:
    """The section name a section key holds; None for a comment thread's root."""
    return key.split(":", 2)[2] if key.startswith(_SECTION) else None


def section_woken(thread_id: str) -> None:
    with _lock:
        _section_wakes.add(thread_id)


def may_answer_section(thread_id: str) -> bool:
    with _lock:
        return thread_id in _section_wakes


def section_answered(thread_id: str) -> None:
    with _lock:
        _section_wakes.discard(thread_id)


def person_holding(root_id: str) -> PersonHold | None:
    """The person whose page is on this thread, if any."""
    with _lock:
        pages = _people.get(root_id)
        return next(iter(pages.values()), None) if pages else None


def agent_writing(root_id: str) -> str | None:
    """The agent whose write call is landing on this thread right now."""
    with _lock:
        return _agents.get(root_id)


def is_free(root_id: str) -> bool:
    with _lock:
        return not _people.get(root_id) and root_id not in _agents


def take(page: object, root_id: str, hold: PersonHold) -> list[str]:
    """``page`` is now on ``root_id``; returns the thread it left, if that freed it."""
    with _lock:
        freed = _release(page) if _page_root.get(page) not in (None, root_id) else []
        _people.setdefault(root_id, {})[page] = hold
        _page_root[page] = root_id
        return freed


def release(page: object) -> list[str]:
    """``page`` left its thread (another thread, panel closed, sent, hidden, idle, gone).

    Returns the thread when no one holds it any more, so its line can be served.
    """
    with _lock:
        return _release(page)


def _release(page: object) -> list[str]:
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
    with _lock:
        return _page_root.get(page)


def people_on(root_id: str) -> set[str]:
    """The users with a page on this thread."""
    with _lock:
        return {hold.user_id for hold in _people.get(root_id, {}).values()}


def agent_claim(root_id: str, thread_id: str) -> bool:
    """Give this agent the turn unless a person or another agent holds it; check and claim are one step."""
    with _lock:
        if _people.get(root_id) or _agents.get(root_id, thread_id) != thread_id:
            return False
        _agents[root_id] = thread_id
        return True


def agents_claim(root_ids: list[str], thread_id: str) -> bool:
    """Give this agent every one of these turns, or none: False when a person or another agent holds any."""
    with _lock:
        if any(_people.get(root_id) or _agents.get(root_id, thread_id) != thread_id for root_id in root_ids):
            return False
        _agents.update(dict.fromkeys(root_ids, thread_id))
        return True


def agent_exit(root_id: str, thread_id: str) -> None:
    with _lock:
        if _agents.get(root_id) == thread_id:
            _agents.pop(root_id, None)


def reset() -> None:
    """Forget every turn, as a restart does (tests)."""
    with _lock:
        _people.clear()
        _page_root.clear()
        _agents.clear()
        _section_wakes.clear()
