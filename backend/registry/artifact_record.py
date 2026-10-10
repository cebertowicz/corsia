"""Unified artifact-record accessor for the coordination MVP (S2).

``register_or_resolve`` returns ONE ``ArtifactRecord`` shape for both first
registration and later resolution (no first-vs-resolve fork). Subscriber kinds
(``owner`` / ``subscriber`` / ``parent_chain``) project from the registration
``relationship_state``; ``parent_chain`` is digest-only and can never become
owner. This is a thin accessor over the existing registration storage
(``artifact_identities`` / ``artifact_registrations``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infra.database import get_db, write_transaction
from registry import models
from registry.identity_bridge import (
    PARENT_CHAIN_REGISTRATION_STATE,
    ParentChainOwnershipError,
    _upsert_registration,
)
from registry.registration_events import _now, routing_registrations

OWNER_KIND = "owner"
SUBSCRIBER_KIND = "subscriber"
PARENT_CHAIN_KIND = "parent_chain"


@dataclass(frozen=True)
class Subscriber:
    """One thread's relationship to an artifact."""

    thread: str
    kind: str  # owner | subscriber | parent_chain


@dataclass(frozen=True)
class ArtifactRecord:
    """The single artifact-routing record (first-register and resolve share it)."""

    identity: dict[str, Any]              # {integration, type, external_id, id?}
    owner_thread: str | None
    subscribers: tuple[Subscriber, ...]
    lifecycle: str                        # active | archived | tombstone | unregistered

    def kind_of(self, thread: str) -> str | None:
        """The kind a thread holds, or None if it has no relationship."""
        for sub in self.subscribers:
            if sub.thread == thread:
                return sub.kind
        return None


def _itu(identity: dict[str, Any]) -> tuple[str, str, str]:
    return (identity["integration"], identity["type"], identity["external_id"])


def _parent_chain_threads(_db, artifact_id: str) -> list[str]:
    rows = _db.execute(
        "SELECT parent_thread_id FROM artifact_registrations "
        "WHERE artifact_id=? AND relationship_state=?",
        (artifact_id, PARENT_CHAIN_REGISTRATION_STATE),
    ).fetchall()
    return [r["parent_thread_id"] for r in rows]


def _identity_row(_db, identity: dict[str, Any]) -> dict | None:
    """The artifact_identities row for an identity in ANY lifecycle state."""
    integration, type_, external_id = _itu(identity)
    row = _db.execute(
        "SELECT * FROM artifact_identities "
        "WHERE integration=? AND type=? AND external_id=?",
        (integration, type_, external_id),
    ).fetchone()
    return dict(row) if row else None


def identity_row_for(artifact_id: str, *, db=None) -> dict | None:
    """Resolve an artifact identity row by internal id or external id."""
    _db = db or get_db()
    try:
        row = _db.execute(
            "SELECT * FROM artifact_identities WHERE id=? OR external_id=?",
            (artifact_id, artifact_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if not db:
            _db.close()


def _build_record(_db, identity: dict[str, Any]) -> ArtifactRecord:
    """Project current registration rows into one ArtifactRecord."""
    id_row = _identity_row(_db, identity)
    if id_row is None:
        return ArtifactRecord(identity=dict(identity), owner_thread=None,
                              subscribers=(), lifecycle="unregistered")

    full_identity = {"integration": id_row["integration"], "type": id_row["type"],
                     "external_id": id_row["external_id"], "id": id_row["id"]}
    # A non-active identity (archived/tombstone) has no live routing; report its
    # lifecycle without owner/subscriber projection.
    if id_row["state"] != "active":
        return ArtifactRecord(identity=full_identity, owner_thread=None,
                              subscribers=(), lifecycle=id_row["state"])

    routing = routing_registrations(*_itu(identity), db=_db)
    owner = routing.get("owner")
    owner_thread = owner["parent_thread_id"] if owner else None

    subs: list[Subscriber] = []
    if owner_thread is not None:
        subs.append(Subscriber(owner_thread, OWNER_KIND))
    for row in routing.get("subscribers") or []:
        subs.append(Subscriber(row["parent_thread_id"], SUBSCRIBER_KIND))
    for thread in _parent_chain_threads(_db, id_row["id"]):
        subs.append(Subscriber(thread, PARENT_CHAIN_KIND))

    return ArtifactRecord(identity=full_identity, owner_thread=owner_thread,
                          subscribers=tuple(subs), lifecycle=id_row["state"])


def register_or_resolve(
    identity: dict[str, Any], owner_intent: str | None = None, *, db=None
) -> ArtifactRecord:
    """Resolve an artifact, or register it to ``owner_intent`` on first sight.

    Idempotent and single-shaped: the returned ``ArtifactRecord`` is identical
    in structure whether the artifact already existed or was just created. A
    ``parent_chain`` thread can never be supplied as ``owner_intent``.
    """
    _db = db or get_db()
    try:
        existing = _build_record(_db, identity)
        # Resolve-only for an identity that already exists (including ownerless
        # after release): re-owning is the explicit, audited rehome path, never a
        # side effect of resolve. This is also WHY register_or_resolve can never
        # promote a parent_chain subscriber to owner — it only ever assigns an
        # owner when first-registering a brand-new identity, which has no
        # subscribers yet. (Rehome enforces the invariant for the explicit path.)
        if existing.lifecycle != "unregistered" or owner_intent is None:
            return existing

        integration, type_, external_id = _itu(identity)
        with write_transaction(_db, scope="register_artifact_record") as connection:
            models.register_artifact(
                integration=integration, type=type_, external_id=external_id,
                parent_thread_id=owner_intent, db=connection,
            )
        return _build_record(_db, identity)
    finally:
        if not db:
            _db.close()


def subscribe_parent_chain(
    identity: dict[str, Any], thread_id: str, *, db=None
) -> ArtifactRecord:
    """Attach a digest-only parent_chain subscriber to a registered artifact."""
    with write_transaction(db, scope="subscribe_parent_chain") as _db:
        record = _build_record(_db, identity)
        if record.lifecycle == "unregistered":
            raise ValueError(
                f"cannot parent_chain-subscribe to unregistered {_itu(identity)}")
        if thread_id == record.owner_thread:
            raise ParentChainOwnershipError(
                f"thread {thread_id} owns this artifact; it cannot also be a "
                "parent_chain subscriber")
        _upsert_registration(
            _db, artifact_id=record.identity["id"], parent_thread_id=thread_id,
            relationship_state=PARENT_CHAIN_REGISTRATION_STATE,
            now=_now(),
        )
        return _build_record(_db, identity)


def remove_subscription(
    identity: dict[str, Any], thread_id: str, *, db=None
) -> ArtifactRecord:
    """Hard-DELETE one thread's subscription/view row — NOT a soft archive.

    Named for what it does: it removes the thread's registration row entirely.
    This is distinct from artifact archive (``models.retire``), which is a soft
    lifecycle state for everyone; this touches only ``thread_id``'s relationship
    and never the artifact's lifecycle. The owner's registration is not removable
    this way (use the audited release/rehome path).
    """
    with write_transaction(db, scope="remove_subscription") as _db:
        record = _build_record(_db, identity)
        if record.lifecycle == "unregistered":
            raise ValueError(f"no artifact to unsubscribe from: {_itu(identity)}")
        if thread_id == record.owner_thread:
            raise ParentChainOwnershipError(
                f"thread {thread_id} is the owner; change its registration via "
                "release/rehome, not remove_subscription")
        _db.execute(
            "DELETE FROM artifact_registrations "
            "WHERE artifact_id=? AND parent_thread_id=?",
            (record.identity["id"], thread_id),
        )
        return _build_record(_db, identity)
