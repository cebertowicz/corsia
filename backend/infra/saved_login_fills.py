"""The person's timed yes before an agent's saved login or saved item is filled into a page.

An agent may have a saved login (``infra.saved_logins``) typed into a page only
under a grant: the person approved this agent's thread filling this login, for
a duration they chose, at most :data:`MAX_GRANT_SECONDS`, or until they turn it
off. There is no automatic approval of any kind, and the host-access reviewer
never decides a fill.

- :func:`request_fill` raises a pending approval; server code calls it, no
  route or tool reaches it. It may carry the timed duration the agent's task
  needs, which the drawer starts on; an agent can never ask for a standing grant.
- :func:`approve_fill`, :func:`deny_fill` and :func:`revoke_fill` are the
  person's, behind ``require_human_user``. Only :func:`approve_fill` makes a
  standing grant (``until_revoked``): no deadline; it ends on revoke, when
  the login is deleted or out of date (:func:`end_for_login`), or at a restart.
- :func:`listed` is the chat's lease list: waiting requests and timed grants
  only. A standing grant is never listed, so no chip shows or opens one.
- :func:`may_fill` is the fill executor's check: true only under a live grant
  for exactly that thread and login. A grant is not a host-access lease.

A saved item (``infra.saved_items``) goes through this same store, decisions,
list, events and durations: ``infra.saved_item_fills`` raises its request and
reads its grant. Each approval's ``kind`` says which it is, ``'login'`` or
``'item'``; an item's approval has no login (``login_id`` None) and carries the
entry's name and label, and its site is the one the person approved, not one a
login was saved for. Code that acts on a site when a grant starts or ends (such
as signing the person out of a login's site) acts only on ``kind == 'login'``:
CORSIA never signed in anywhere for an item, so its grant has no such effect.

Thread identity (C3, ``infra.saved_fill_identity``): the thread id comes from the
call's server-side context, never a tool argument; inside an agent's turn any
other thread is refused.

Requests and grants live in this process's memory only, standing grants
included, so a restart fails closed until the person approves again. Nothing
here reads or holds a password or an item's value.
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
from infra.saved_fill_identity import (
    FillApprovalError as FillApprovalError, FillApprovalNotFound as FillApprovalNotFound,
    FillApprovalNotTheirs as FillApprovalNotTheirs, FillDurationError as FillDurationError,
    context_thread as _context_thread, own_thread as _own_thread, thread_record as thread_record,
)
from infra.time import utc_now, utc_now_iso

#: The longest grant: the host-access lease cap, the existing limit on any
#: single approval the person gives an agent.
MAX_GRANT_SECONDS = MAX_LEASE_DURATION_SECONDS
#: The shortest grant, the same floor the host-access lease takes.
MIN_GRANT_SECONDS = 1

AWAITING_USER = "awaiting_user"
ACTIVE = "active"
#: An approval's kind: a saved login, or a saved item (``infra.saved_items``).
LOGIN = "login"
ITEM = "item"

_NO_REQUEST = "This sign-in request is closed. Ask the agent to try again if it still needs to sign in."
_NO_GRANT = "This approval has ended."
_NOT_CURRENT = "This request has new details. Review them, then approve or reject it."
_DURATION = f"Pick a duration from {MIN_GRANT_SECONDS} to {MAX_GRANT_SECONDS} seconds."
_OUT_OF_DATE = "The saved login for {site} ({username}) is out of date. The person has a step to sign in to {site} themselves."
_NOT_THEIRS = "No saved login you may use has that id."


@dataclass(frozen=True)
class _Approval:
    id: str
    thread_id: str
    root_thread_id: str
    user_id: str
    agent: str
    #: The saved login's id; None for an item.
    login_id: str | None
    #: The site the value may be filled on: the login's own site, or the one the person approved for an item.
    site: str
    #: The login's username; empty for an item.
    username: str
    created_at: str
    updated_at: str
    revision: int
    state: str
    duration_seconds: int | None = None
    expires_at: str | None = None
    #: Monotonic clock deadline; a wall-clock change can't stretch a grant.
    deadline: float | None = None
    #: The duration the agent asked for, which the person's drawer starts on; never a standing grant.
    requested_duration_seconds: int | None = None
    #: The person chose "Until I turn it off": an active grant with no deadline.
    standing: bool = False
    #: LOGIN or ITEM. Only a login's grant may act on its site when it starts or ends.
    kind: str = LOGIN
    #: An item's key store entry name and its label; None for a login.
    item: str | None = None
    item_label: str | None = None

    @property
    def application_hash(self) -> str:
        fields = [self.id, self.thread_id, self.login_id, self.site, self.username, self.created_at]
        if self.kind == ITEM:
            fields += [self.kind, self.item, self.item_label]
        return hashlib.sha256(json.dumps(fields).encode("utf-8")).hexdigest()

    def live(self, now: float) -> bool:
        if self.state == AWAITING_USER:
            return True
        return self.state == ACTIVE and (self.standing or (self.deadline is not None and now < self.deadline))


_lock = threading.Lock()
_approvals: dict[str, _Approval] = {}


def _prune(now: float) -> list[_Approval]:
    """Drop what has run out and return it. Only a grant runs out; a waiting request stays until decided."""
    expired = [item for item in _approvals.values() if not item.live(now)]
    for item in expired:
        del _approvals[item.id]
    return expired


def _emit(action: str, item: _Approval, properties: dict[str, Any] | None = None) -> None:
    """Count a fill transition (thread, minutes; never site, username or id). Called outside the lock."""
    product_events.emit_product_event("saved_login", action, thread_id=item.thread_id, properties=properties)


def _emit_expired(expired: list[_Approval]) -> None:
    """A grant is recorded as expired when the next read drops it; a restart first drops it uncounted."""
    for item in expired:
        _emit("fill_expired", item)


def _minutes(seconds: int) -> int | float:
    return seconds // 60 if seconds % 60 == 0 else round(seconds / 60, 2)


def _login_usable(login_id: str, user_id: str) -> bool:
    """Not out of date, and saved by the person whose agent holds the grant."""
    try:
        login = saved_logins.get_login(login_id)
    except KeyStoreError:
        return False
    return login.status == saved_logins.STATUS_OK and login.saved_by == user_id


def _remove(predicate) -> list[_Approval]:
    with _lock:
        ended = [item for item in _approvals.values() if predicate(item)]
        for item in ended:
            del _approvals[item.id]
    return ended


def end_for_login(login_id: str, *, deleted: bool) -> None:
    """End every request and grant for the login, standing ones included: ``fill_revoked`` on a delete,
    ``fill_expired`` when it goes out of date. The paths that make a login unusable call this as they do,
    so a re-save can't revive a grant."""
    _ended(_remove(lambda item: item.kind == LOGIN and item.login_id == login_id), deleted)


def _ended(ended: list[_Approval], deleted: bool) -> None:
    for item in ended:
        _emit("fill_revoked" if deleted else "fill_expired", item)
        logger.info("saved-{} fill ended with its {}: approval {}", item.kind, item.kind, item.id)
        _notify(item)


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
        "fill_kind": item.kind,
        "item_label": item.item_label,
        "revision": item.revision,
        "application_hash": item.application_hash,
        "max_duration_seconds": MAX_GRANT_SECONDS,
        "requested_duration_seconds": item.requested_duration_seconds,
        "duration_seconds": item.duration_seconds,
        "standing": item.standing,
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


def request_fill(thread_id: str, login_id: str, *, requested_seconds: object = None) -> dict[str, Any]:
    """Ask the person to approve this thread filling this saved login; return the request.

    ``requested_seconds`` is ``None`` or whole seconds within the cap; anything else, a standing
    grant included, raises :class:`FillDurationError` (refused, not clamped, as a lease's is).
    A request waiting or grant live for the same thread and login is returned as it is.
    """
    requested = None if requested_seconds is None else _duration(requested_seconds)
    clean_thread = _own_thread(thread_id)
    try:
        login = saved_logins.get_login(login_id)
    except KeyStoreError as exc:
        raise FillApprovalError(str(exc)) from None
    user_id, root, agent = thread_record(clean_thread)
    if login.saved_by != user_id:
        raise FillApprovalNotTheirs(_NOT_THEIRS)
    if login.status != saved_logins.STATUS_OK:
        raise FillApprovalError(_OUT_OF_DATE.format(site=login.site, username=login.username))
    return open_request(
        clean_thread, (user_id, root, agent), requested,
        lambda item: item.kind == LOGIN and item.login_id == login.id,
        login_id=login.id, site=login.site, username=login.username,
    )


def open_request(clean_thread: str, record: tuple[str, str, str], requested: int | None, same, **fields: Any,
                 ) -> dict[str, Any]:
    """Raise a waiting request with ``fields`` (login or item) for an already-checked thread, or return the
    request waiting or grant live for this thread that ``same`` matches, as it is. For this module and
    ``infra.saved_item_fills`` only."""
    user_id, root, agent = record
    now_iso = utc_now_iso()
    with _lock:
        expired = _prune(time.monotonic())
        existing = next((item for item in _approvals.values() if item.thread_id == clean_thread and same(item)), None)
        if existing is None:
            item = _Approval(
                id=uuid.uuid4().hex, thread_id=clean_thread, root_thread_id=root, user_id=user_id, agent=agent,
                created_at=now_iso, updated_at=now_iso, revision=1, state=AWAITING_USER,
                requested_duration_seconds=requested, **fields,
            )
            _approvals[item.id] = item
    _emit_expired(expired)
    if existing is not None:
        return _public(existing)
    _emit("fill_requested", item, None if requested is None else {"requested_minutes": _minutes(requested)})
    logger.info("saved-{} fill requested: approval {} thread {}", item.kind, item.id, clean_thread)
    _notify(item)
    return _public(item)


def may_fill(thread_id: str, login_id: str) -> bool:
    """True only while the person's grant for exactly this thread and login is live, the running
    turn is that thread, and the login is usable and theirs, so a grant never retries a refused password."""
    if not isinstance(login_id, str) or not login_id:
        return False
    grant = live_grant(thread_id, lambda item: item.kind == LOGIN and item.login_id == login_id)
    if grant is None:
        return False
    if _login_usable(login_id, grant.user_id):
        return True
    end_for_login(login_id, deleted=False)
    return False


def live_grant(thread_id: object, same) -> _Approval | None:
    """The live grant for this thread that ``same`` matches, when the running turn is that thread; else None.
    For this module and ``infra.saved_item_fills`` only."""
    if not isinstance(thread_id, str) or not thread_id:
        return None
    running = _context_thread()
    if running is not None and running != thread_id:
        return None
    now = time.monotonic()
    with _lock:
        return next((
            item for item in _approvals.values()
            if item.state == ACTIVE and item.live(now) and item.thread_id == thread_id and same(item)
        ), None)


def end_where(same, *, deleted: bool) -> None:
    """End every request and grant ``same`` matches, as :func:`end_for_login` does. For ``infra.saved_item_fills``."""
    _ended(_remove(same), deleted)


def listed(user_id: str, root_thread_id: str | None = None) -> list[dict[str, Any]]:
    """This person's waiting requests and live timed grants, oldest first; one chat's when a root is named.

    A standing grant is left out: the chat's lock chip is for timed grants only.
    """
    with _lock:
        expired = _prune(time.monotonic())
        items = [
            item for item in _approvals.values()
            if item.user_id == user_id and not item.standing
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
    if item is None or item.user_id != user_id or item.state != state or not item.live(time.monotonic()):
        raise FillApprovalNotFound(_NO_REQUEST if state == AWAITING_USER else _NO_GRANT)
    if item.revision != revision or item.application_hash != application_hash:
        raise FillApprovalError(_NOT_CURRENT)
    return item


def _duration(seconds: object) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not MIN_GRANT_SECONDS <= seconds <= MAX_GRANT_SECONDS:
        raise FillDurationError(_DURATION)
    return seconds


def approve_fill(
    approval_id: str, *, user_id: str, revision: int, application_hash: str, duration_seconds: object = None,
    until_revoked: object = False,
) -> dict[str, Any]:
    """The person's yes: exactly one of ``duration_seconds`` within the cap, or ``until_revoked=True``."""
    if until_revoked is True and duration_seconds is None:
        seconds = None
    elif until_revoked is False:
        seconds = _duration(duration_seconds)
    else:
        raise FillDurationError(_DURATION)
    with _lock:
        item = _current(approval_id, user_id, AWAITING_USER, revision, application_hash)
        now_iso = utc_now_iso()
        if seconds is None:
            item = replace(item, state=ACTIVE, revision=item.revision + 1, updated_at=now_iso, standing=True)
        else:
            item = replace(
                item, state=ACTIVE, revision=item.revision + 1, updated_at=now_iso, duration_seconds=seconds,
                expires_at=(utc_now() + timedelta(seconds=seconds)).isoformat(),
                deadline=time.monotonic() + seconds,
            )
        _approvals[item.id] = item
    _emit("fill_approved", item, {"standing": True} if seconds is None else {"duration_minutes": _minutes(seconds)})
    logger.info("saved-login fill approved: approval {} for {}", item.id, "until revoked" if seconds is None else f"{seconds}s")
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
