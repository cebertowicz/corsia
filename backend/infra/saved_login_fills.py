"""The person's timed yes before an agent's saved login is filled into a page.

An agent may have a saved login (``infra.saved_logins``) typed into a page only
under a grant: the person approved this agent's thread filling this login, for
a duration they chose, at most :data:`MAX_GRANT_SECONDS`. There is no automatic
approval of any kind, and the host-access reviewer never decides a fill.

- :func:`request_fill` raises a pending approval. Server code calls it, never
  an agent: no route or tool reaches it. It names the agent from the server's
  own thread record and the site and username from the login's public view.
- :func:`approve_fill`, :func:`deny_fill` and :func:`revoke_fill` are the
  person's, reached only through routes behind ``require_human_user``.
- :func:`may_fill` is the fill executor's check: true only under a live grant
  for exactly that thread and login.

A grant is not a host-access lease. It opens :func:`may_fill` and nothing else.

Thread identity (C3): the thread id comes from the server-side context of the
call, never from a tool argument. When one of these functions runs inside an
agent's turn, the thread id it is given must be that turn's own thread; a
different one is refused, so a caller cannot act for another thread.

Requests and grants live in this process's memory only. A restart drops them
all, so the server comes back failing closed: :func:`may_fill` is false until
the person approves again. Nothing here reads or holds a password.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from loguru import logger

from host_access.browser_domain import url_domain
from infra import product_events, saved_logins
from infra.host_access_leases import MAX_LEASE_DURATION_SECONDS
from infra.key_store_values import KeyStoreError
from infra.time import utc_now, utc_now_iso

#: The longest grant: the host-access lease cap, the existing limit on any
#: single approval the person gives an agent.
MAX_GRANT_SECONDS = MAX_LEASE_DURATION_SECONDS
#: The shortest grant, the same floor the host-access lease takes.
MIN_GRANT_SECONDS = 1

AWAITING_USER = "awaiting_user"
ACTIVE = "active"

_NO_REQUEST = "This sign-in request is closed. Ask the agent to try again if it still needs to sign in."
_NO_GRANT = "This approval has ended."
_NOT_CURRENT = "This request has new details. Review them, then approve or reject it."
_NO_THREAD = "A fill request needs the agent's own thread."
_OTHER_THREAD = "A fill request can only be made for the agent's own thread."
_DURATION = f"Pick a duration from {MIN_GRANT_SECONDS} to {MAX_GRANT_SECONDS} seconds."
_OUT_OF_DATE = "The saved login for {site} ({username}) is out of date. The person has a step to sign in to {site} themselves."
_AGENT_FALLBACK = "An agent"


class FillApprovalError(ValueError):
    """A fill request or decision was refused. The text is safe to show: at most a site and username, never a password."""


class FillApprovalNotFound(FillApprovalError):
    """No such request or grant for this person."""


class FillDurationError(FillApprovalError):
    """The duration is not a whole number of seconds within the cap; nothing was granted."""


@dataclass(frozen=True)
class _Approval:
    id: str
    thread_id: str
    root_thread_id: str
    user_id: str
    agent: str
    login_id: str
    site: str
    username: str
    created_at: str
    updated_at: str
    revision: int
    state: str
    duration_seconds: int | None = None
    expires_at: str | None = None
    #: Monotonic clock deadline; a wall-clock change can't stretch a grant.
    deadline: float | None = None

    @property
    def application_hash(self) -> str:
        fields = [self.id, self.thread_id, self.login_id, self.site, self.username, self.created_at]
        return hashlib.sha256(json.dumps(fields).encode("utf-8")).hexdigest()

    def live(self, now: float) -> bool:
        return self.state == AWAITING_USER or (self.deadline is not None and now < self.deadline)


_lock = threading.Lock()
_approvals: dict[str, _Approval] = {}


def _context_thread() -> str | None:
    from infra import state

    return state._current_thread_id.get(None)


def _own_thread(thread_id: object) -> str:
    """The thread id, refused when it is empty or names a thread other than the running turn's own."""
    if not isinstance(thread_id, str) or not thread_id:
        raise FillApprovalError(_NO_THREAD)
    running = _context_thread()
    if running is not None and running != thread_id:
        raise FillApprovalError(_OTHER_THREAD)
    return thread_id


def _thread_record(thread_id: str) -> tuple[str, str, str]:
    """The thread's owner, its visible root and the agent's name, from the server's own records."""
    from core.thread_identity import root_visible_thread_id
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute("SELECT user_id, title FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if row is None:
            raise FillApprovalError(_NO_THREAD)
        root = root_visible_thread_id(thread_id, db=db) or thread_id
    finally:
        db.close()
    title = row["title"].strip() if isinstance(row["title"], str) else ""
    return row["user_id"], root, title or _AGENT_FALLBACK


def _prune(now: float) -> list[_Approval]:
    """Drop what has run out and return it. Only a grant runs out; a waiting request stays until decided."""
    expired = [item for item in _approvals.values() if not item.live(now)]
    for item in expired:
        del _approvals[item.id]
    return expired


def _emit(action: str, item: _Approval, properties: dict[str, Any] | None = None) -> None:
    """Count a fill transition: the thread and, on approve, the minutes picked. Never the site, username or id.

    Called outside the lock. A route runs these functions in a worker thread, so the write never blocks
    the event loop.
    """
    product_events.emit_product_event("saved_login", action, thread_id=item.thread_id, properties=properties)


def _emit_expired(expired: list[_Approval]) -> None:
    """A grant is recorded as expired when the next read drops it; a restart first drops it uncounted."""
    for item in expired:
        _emit("fill_expired", item)


def _public(item: _Approval) -> dict[str, Any]:
    """What the person's surface shows: never a password, never the login's secret."""
    return {
        "id": item.id,
        "kind": "fill",
        "state": item.state,
        "thread_id": item.thread_id,
        "root_thread_id": item.root_thread_id,
        "agent": item.agent,
        "site": item.site,
        #: The site's registrable domain from the Public Suffix List, so a shortened site always keeps it whole.
        "domain": url_domain(item.site),
        "username": item.username,
        "revision": item.revision,
        "application_hash": item.application_hash,
        "max_duration_seconds": MAX_GRANT_SECONDS,
        "duration_seconds": item.duration_seconds,
        "expires_at": item.expires_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _notify(item: _Approval) -> None:
    """Nudge the person's open chats to re-read their approvals; best effort."""
    try:
        from host_access.change_nudge import changed_message
        from infra.state import ws

        ws.broadcast_to_user(item.user_id, changed_message({"id": item.id, "root_thread_id": item.root_thread_id}))
    except Exception as exc:  # noqa: BLE001 - a missed nudge leaves the next read correct
        logger.warning("saved-login fill approval {}: change nudge not sent: {}", item.id, type(exc).__name__)


def request_fill(thread_id: str, login_id: str) -> dict[str, Any]:
    """Ask the person to approve this thread filling this saved login; return the request.

    Called by server code only, with the thread id from the executing turn's
    context. A request already waiting, or a grant still live, for the same
    thread and login is returned as it is rather than raising another.
    """
    clean_thread = _own_thread(thread_id)
    try:
        login = saved_logins.get_login(login_id)
    except KeyStoreError as exc:
        raise FillApprovalError(str(exc)) from None
    if login.status != saved_logins.STATUS_OK:
        raise FillApprovalError(_OUT_OF_DATE.format(site=login.site, username=login.username))
    user_id, root, agent = _thread_record(clean_thread)
    now_iso = utc_now_iso()
    existing: _Approval | None = None
    with _lock:
        expired = _prune(time.monotonic())
        existing = next(
            (item for item in _approvals.values() if item.thread_id == clean_thread and item.login_id == login.id),
            None,
        )
        if existing is None:
            item = _Approval(
                id=uuid.uuid4().hex, thread_id=clean_thread, root_thread_id=root, user_id=user_id, agent=agent,
                login_id=login.id, site=login.site, username=login.username,
                created_at=now_iso, updated_at=now_iso, revision=1, state=AWAITING_USER,
            )
            _approvals[item.id] = item
    _emit_expired(expired)
    if existing is not None:
        return _public(existing)
    _emit("fill_requested", item)
    logger.info("saved-login fill requested: approval {} thread {} login {}", item.id, clean_thread, login.id)
    _notify(item)
    return _public(item)


def may_fill(thread_id: str, login_id: str) -> bool:
    """True only while the person's grant for exactly this thread and login is live.

    False before approval, after it runs out, after a deny or a revoke, for any
    other thread or login, for a thread other than the running turn's own, and
    once the login is missing or out of date, so a live grant never retries a
    password the site already refused.
    """
    if not isinstance(thread_id, str) or not isinstance(login_id, str) or not thread_id or not login_id:
        return False
    running = _context_thread()
    if running is not None and running != thread_id:
        return False
    now = time.monotonic()
    with _lock:
        granted = any(
            item.state == ACTIVE and item.deadline is not None and now < item.deadline
            and item.thread_id == thread_id and item.login_id == login_id
            for item in _approvals.values()
        )
    if not granted:
        return False
    try:
        return saved_logins.get_login(login_id).status == saved_logins.STATUS_OK
    except KeyStoreError:
        return False


def listed(user_id: str, root_thread_id: str | None = None) -> list[dict[str, Any]]:
    """This person's waiting requests and live grants, oldest first; one chat's when a root is named."""
    with _lock:
        expired = _prune(time.monotonic())
        items = [
            item for item in _approvals.values()
            if item.user_id == user_id
            and (root_thread_id is None or root_thread_id in (item.root_thread_id, item.thread_id))
        ]
    _emit_expired(expired)
    items.sort(key=lambda item: (item.created_at, item.id))
    return [_public(item) for item in items]


def get(approval_id: str, *, user_id: str) -> dict[str, Any]:
    """One of this person's requests or grants, or :class:`FillApprovalNotFound`."""
    with _lock:
        expired = _prune(time.monotonic())
        item = _approvals.get(approval_id)
    _emit_expired(expired)
    if item is None or item.user_id != user_id:
        raise FillApprovalNotFound(_NO_REQUEST)
    return _public(item)


def _current(approval_id: str, user_id: str, state: str, revision: int, application_hash: str) -> _Approval:
    item = _approvals.get(approval_id)
    if item is None or item.user_id != user_id or not item.live(time.monotonic()):
        raise FillApprovalNotFound(_NO_REQUEST if state == AWAITING_USER else _NO_GRANT)
    if item.state != state:
        raise FillApprovalNotFound(_NO_REQUEST if state == AWAITING_USER else _NO_GRANT)
    if item.revision != revision or item.application_hash != application_hash:
        raise FillApprovalError(_NOT_CURRENT)
    return item


def _duration(seconds: object) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise FillDurationError(_DURATION)
    if not MIN_GRANT_SECONDS <= seconds <= MAX_GRANT_SECONDS:
        raise FillDurationError(_DURATION)
    return seconds


def approve_fill(
    approval_id: str, *, user_id: str, revision: int, application_hash: str, duration_seconds: object,
) -> dict[str, Any]:
    """The person's yes: the grant runs for ``duration_seconds``, refused above :data:`MAX_GRANT_SECONDS`."""
    seconds = _duration(duration_seconds)
    with _lock:
        item = _current(approval_id, user_id, AWAITING_USER, revision, application_hash)
        now_iso = utc_now_iso()
        item = replace(
            item, state=ACTIVE, revision=item.revision + 1, updated_at=now_iso, duration_seconds=seconds,
            expires_at=(utc_now() + timedelta(seconds=seconds)).isoformat(),
            deadline=time.monotonic() + seconds,
        )
        _approvals[item.id] = item
    _emit("fill_approved", item, {"duration_minutes": seconds // 60 if seconds % 60 == 0 else round(seconds / 60, 2)})
    logger.info("saved-login fill approved: approval {} for {}s", item.id, seconds)
    _notify(item)
    return _public(item)


def _end(approval_id: str, user_id: str, state: str, revision: int, application_hash: str, verb: str) -> dict[str, Any]:
    with _lock:
        item = _current(approval_id, user_id, state, revision, application_hash)
        del _approvals[item.id]
    _emit(f"fill_{verb}", item)
    logger.info("saved-login fill {}: approval {}", verb, item.id)
    _notify(item)
    ended = _public(item)
    ended["state"] = verb
    return ended


def deny_fill(approval_id: str, *, user_id: str, revision: int, application_hash: str) -> dict[str, Any]:
    """The person's no: the request ends and nothing is granted."""
    return _end(approval_id, user_id, AWAITING_USER, revision, application_hash, "denied")


def revoke_fill(approval_id: str, *, user_id: str, revision: int, application_hash: str) -> dict[str, Any]:
    """The person ends a live grant early; :func:`may_fill` is false from this moment."""
    return _end(approval_id, user_id, ACTIVE, revision, application_hash, "revoked")
