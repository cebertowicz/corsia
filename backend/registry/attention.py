"""User-attention lifecycle and passive closeout recording.

A raised user-attention item is artifact LIFECYCLE STATE (``attention_state`` on
the artifact), never an Event Delivery disposition. Closeout clears that single
state and records one subscriber-independent passive change. It never wakes and
never archives the artifact or any subscription. Reading a raised item does not
clear it (close-viewer != clear). Defer records the concrete condition produced
by the parent gate (S5b) for S7 to arm later; it does not arm a watch here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from infra.database import get_db, write_transaction
from infra.product_events import emit_product_event
from registry import artifact_record as AR
from registry import models

ATTENTION_RAISED = "raised"
ATTENTION_DEFERRED = "deferred"
ATTENTION_CLEARED = "cleared"

#: Explicit clearing actions (AC-4.2). Closing a viewer is NOT one of these.
CLEAR_OUTCOMES = frozenset({"answer", "acknowledge", "dismiss", "supersede", "defer"})
RETIRE_OUTCOME = "retire"
WITHDRAW_OUTCOME = "withdraw"

DEFER_CONDITION_TYPES = frozenset({"deadline", "event", "milestone", "dependency"})


class AttentionError(ValueError):
    """An invalid attention operation (unknown artifact/outcome/condition)."""


@dataclass(frozen=True)
class AttentionResult:
    """The durable outcome of a closeout — distinct from archive/subscription ops."""

    artifact_id: str
    outcome: str
    attention_state: str
    cleared_levels: tuple[str, ...]
    artifact_archived: bool = False        # closeout NEVER archives the artifact
    subscription_archived: bool = False    # closeout NEVER archives a subscription
    defer_condition: dict | None = None
    #: The recorded review verdict, its note and the round's counts, once answered.
    review: dict | None = None


def _identity_row(artifact_id: str, db) -> dict:
    row = AR.identity_row_for(artifact_id, db=db)
    if row is None:
        raise AttentionError(f"artifact {artifact_id!r} is not registered")
    return row


def _identity(row: dict) -> dict:
    return {"integration": row["integration"], "type": row["type"],
            "external_id": row["external_id"]}


def _attention_state(row: dict) -> str | None:
    meta = json.loads(row["metadata"]) if row.get("metadata") else {}
    return meta.get("attention_state")


def raise_attention(artifact_id: str, *, db=None) -> str:
    """Mark an artifact's attention as raised, optionally inside a caller transaction."""
    with write_transaction(db, scope="raise_attention") as _db:
        identity = _identity(_identity_row(artifact_id, _db))
        models.update_metadata(
            identity["integration"], identity["type"], identity["external_id"],
            {"attention_state": ATTENTION_RAISED}, db=_db,
        )
        return ATTENTION_RAISED


def get_attention_state(artifact_id: str, *, db=None) -> str | None:
    """Read the current attention state. Reading NEVER clears (close-viewer ≠ clear)."""
    _db = db or get_db()
    try:
        return _attention_state(_identity_row(artifact_id, _db))
    finally:
        if not db:
            _db.close()


def list_raised(*, thread_id: str | None = None, db=None) -> list[dict[str, Any]]:
    """Active artifacts whose attention is raised, optionally scoped to a
    subscribed thread. A read — it does not mutate attention state."""
    _db = db or get_db()
    try:
        # Filter to raised rows in SQL (json_extract over metadata — the same
        # convention as registry.bootload) so we only resolve the few raised
        # artifacts, not every active one. This is a read: it never mutates
        # attention state, adds no delivery class, and parent_chain stays
        # digest-only (levels are display data, not a wake).
        rows = _db.execute(
            "SELECT * FROM artifact_identities WHERE state='active' "
            "AND json_extract(metadata, '$.attention_state') = ?",
            (ATTENTION_RAISED,),
        ).fetchall()
        raised: list[dict[str, Any]] = []
        for row in rows:
            rowd = dict(row)
            identity = _identity(rowd)
            record = AR.register_or_resolve(identity, db=_db)
            levels = {s.thread for s in record.subscribers}
            if thread_id is not None and thread_id not in levels:
                continue
            # Return the resolved owner + parsed metadata so callers need not
            # re-resolve (single resolution path).
            raised.append({"identity": identity,
                           "external_id": rowd["external_id"],
                           "title": rowd.get("title"),
                           "owner_thread": record.owner_thread,
                           "levels": sorted(levels),
                           "metadata": json.loads(rowd["metadata"]) if rowd["metadata"] else {}})
        return raised
    finally:
        if not db:
            _db.close()


def clear_attention(
    artifact_id: str, *, outcome: str, condition: dict | None = None,
    reason: str | None = None, verdict: str | None = None, note: str | None = None,
    db=None, drives: list[str] | None = None,
) -> AttentionResult:
    """Clear attention through one of the public user/system outcomes.

    A review ``verdict`` (with its optional ``note``) rides only on ``answer``.
    ``drives`` collects the threads this write gave a plan wake (an answered
    ask's asker), for the caller to drive once the write has committed.
    """
    if outcome not in CLEAR_OUTCOMES:
        raise AttentionError(f"unknown clearing outcome {outcome!r}")
    if (verdict is not None or note is not None) and outcome != "answer":
        raise AttentionError("a verdict or note goes only with the answer outcome")
    if note is not None and verdict is None:
        raise AttentionError("a note needs a verdict")
    return _clear_attention(
        artifact_id, outcome=outcome, condition=condition, reason=reason,
        verdict=verdict, note=note, db=db, drives=drives,
    )


def retire_attention(artifact_id: str, *, db=None) -> AttentionResult:
    """End attention because its artifact retired, never because a user dismissed it."""
    return _clear_attention(
        artifact_id, outcome=RETIRE_OUTCOME, reason="artifact retired", db=db,
    )


def withdraw_attention(artifact_id: str, *, db=None) -> AttentionResult:
    """End attention because the asking task withdrew its review request."""
    return _clear_attention(
        artifact_id, outcome=WITHDRAW_OUTCOME, reason="review request withdrawn", db=db,
    )


def _clear_attention(
    artifact_id: str, *, outcome: str, condition: dict | None = None,
    reason: str | None = None, verdict: str | None = None, note: str | None = None,
    db=None, drives: list[str] | None = None,
) -> AttentionResult:
    """Own the one atomic attention closeout for public clear and retirement.

    Decisive: one attention state is cleared, then one passive change is
    recorded for subscribers to pull on later activity. It never wakes or
    archives the artifact or any subscription. ``defer`` records the concrete
    condition for S7 to arm; it does not arm a watch here.
    """
    from plans import public as plans_public

    with write_transaction(db, scope="clear_attention") as _db, \
            plans_public.collect_wake_recipients(_db, drives):
        row = _identity_row(artifact_id, _db)
        identity = _identity(row)
        record = AR.register_or_resolve(identity, db=_db)
        levels = sorted({s.thread for s in record.subscribers})

        answer = None
        if verdict is not None:
            # Before the state flips: a stale ask refuses, and the round's feedback
            # reaches its owner in this same write.
            from doceditor import review_bar

            answer = review_bar.answer(_db, str(row["id"]), verdict, note)
        defer_condition = None
        if outcome == "defer":
            defer_condition = validate_defer_condition(condition)
            new_state = ATTENTION_DEFERRED
        else:
            new_state = ATTENTION_CLEARED

        patch: dict[str, Any] = {"attention_state": new_state,
                                 "attention_outcome": outcome,
                                 "attention_reason": reason}
        if defer_condition is not None:
            patch["defer_condition"] = defer_condition  # S7 arms from this later
        models.update_metadata(
            identity["integration"], identity["type"], identity["external_id"],
            patch, db=_db,
        )
        _record_closeout(identity, outcome, _db)
        # Attention owns this one state write; who ends the ask decides the
        # order. The user ends it here, and this hook tells Plans, which clears
        # the linked task's hold. A task ends its own ask in Plans
        # (``plans.ask_withdrawal``), which clears the hold first and then calls
        # here, so the hook finds no open hold and does nothing. It is a no-op
        # for ordinary artifacts and decisions, and ``defer`` keeps the hold.
        plans_public.ask_cleared(
            str(row["id"]), outcome, db=_db, release=answer.release() if answer else None,
        )
    if db is None or not db.in_transaction:
        emit_product_event(
            "artifact_attention", "cleared",
            properties={"external_id": identity["external_id"],
                        "outcome": outcome, "levels": len(levels)},
        )
    return AttentionResult(
        artifact_id=artifact_id, outcome=outcome, attention_state=new_state,
        cleared_levels=tuple(levels), artifact_archived=False,
        subscription_archived=False, defer_condition=defer_condition,
        review=answer.payload() if answer else None)


def validate_defer_condition(condition: dict | None) -> dict:
    """The single source of truth for a concrete defer condition (S5b + S6a + S7).

    Returns the condition (with a deadline normalized to canonical UTC ISO) if
    valid; raises ``AttentionError`` otherwise. An unconditioned defer is invalid;
    a deadline must be a parseable, timezone-aware timestamp (so stored/compared
    deadlines share one canonical form and never compare as raw local strings).

    Every category's value is validated here rather than at each caller: it is
    stored in one text column, so an object, array or null would reach SQLite
    unbound and surface as an unhandled binding error instead of the
    caller-fixable refusal this function exists to produce. Enforcing it at a
    caller would leave the other callers exposed and split one rule across two
    owners.
    """
    if not isinstance(condition, dict) or condition.get("type") not in DEFER_CONDITION_TYPES:
        raise AttentionError(
            "defer requires a concrete condition "
            f"{{'type': one of {sorted(DEFER_CONDITION_TYPES)}, 'value': ...}}")
    if condition["type"] == "deadline":
        condition = {**condition, "value": _canonical_deadline(condition.get("value"))}
    elif not (isinstance(condition.get("value"), str) and condition["value"].strip()):
        raise AttentionError(
            f"{condition['type']} defer requires a non-empty string 'value'; "
            f"got {type(condition.get('value')).__name__}")
    return condition


def _canonical_deadline(value) -> str:
    """Normalize a deadline to canonical UTC ISO; fail loud if not parseable/tz-aware."""
    if not value:
        raise AttentionError("deadline defer requires a concrete 'value'")
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttentionError(
            f"deadline value {value!r} is not a valid ISO-8601 timestamp") from exc
    if dt.tzinfo is None:
        raise AttentionError(
            f"deadline value {value!r} must include a timezone (use UTC ISO)")
    return dt.astimezone(timezone.utc).isoformat()


def _record_closeout(identity: dict, outcome: str, db) -> None:
    """Record one passive closeout for subscribers to pull on activity."""
    artifact_id = _artifact_internal_id(identity, db)  # resolve once, reuse per level
    models.record_artifact_change(
        artifact_id=artifact_id,
        internal_event="artifact.attention_cleared",
        external_id=identity["external_id"],
        summary=f"attention cleared: {outcome}",
        provenance={"outcome": outcome, "delivery": "digest", "wake": False},
        db=db,
    )


def _artifact_internal_id(identity: dict, db) -> str:
    row = db.execute(
        "SELECT id FROM artifact_identities WHERE integration=? AND type=? AND external_id=?",
        (identity["integration"], identity["type"], identity["external_id"]),
    ).fetchone()
    return row["id"]
