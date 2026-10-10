"""Expected-event watch lifecycle (coordination MVP S7).

A watch is armed from a concrete condition (a defer deadline recorded by S5b/S6a,
or a missing reviewer/CI window). It persists, resolves at most once if its event
arrives, and expires at most once if it does not. Expiry routes a missing-event
failure through Event Delivery (S4b) as a ``delivery_failure`` — never a silent
success, never a generic dead-letter, never a double-wake. Automation owns the
watch lifecycle; the wake mechanism is consumed from Event Delivery.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from events.resolve import resolve_delivery_failure
from infra.database import TransactionOwnershipError, get_db, write_transaction
from infra.product_events import emit_product_event
from registry import attention
from registry import artifact_record as AR

ARMED = "armed"
RESOLVED = "resolved"
EXPIRED = "expired"


class WatchError(ValueError):
    """An invalid watch operation (unknown watch / artifact / condition)."""


class UnboundWatchError(WatchError):
    """A non-deadline watch named no artifact, so nothing could ever end it (#1707)."""


UNBOUND_REFUSED = (
    "only a deadline watch ends by itself; any other watch ends when its "
    "artifact's event arrives: pass the artifact_id and expected_event, or "
    "wait on a deadline condition instead"
)


@dataclass(frozen=True)
class Watch:
    id: str
    artifact_ref: dict | None
    owner_thread: str | None
    plan_id: str | None
    plan_node_id: str | None
    armed_by_actor_id: str | None
    condition: dict
    expected_event: str | None
    status: str


@dataclass(frozen=True)
class ExpiryResult:
    watch_id: str
    routed_wakes: int          # 1 on the first expiry of an armed watch, else 0
    resolution: str | None     # the Event-Delivery resolution used (delivery_failure)
    target: str | None = None
    already_final: bool = False


def expiry_event(result: ExpiryResult) -> tuple[str, str, dict] | None:
    """Return the existing expiry metric only for a newly routed failure."""
    if result.routed_wakes == 0:
        return None
    return "automation_watch", "expired", {
        "watch_id": result.watch_id, "resolution": result.resolution,
        "target": result.target,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_watch(row: dict) -> Watch:
    ref = None
    if row["artifact_external_id"]:
        ref = {"integration": row["artifact_integration"],
               "type": row["artifact_type"],
               "external_id": row["artifact_external_id"]}
    return Watch(
        id=row["id"], artifact_ref=ref, owner_thread=row["owner_thread_id"],
        plan_id=row["plan_id"], plan_node_id=row["plan_node_id"],
        armed_by_actor_id=row["armed_by_actor_id"],
        condition={"type": row["condition_type"], "value": row["condition_value"]},
        expected_event=row["expected_event"], status=row["status"])


def _resolve_artifact(artifact_id: str, db) -> tuple[dict | None, str | None]:
    """Return (artifact_ref, owner_thread) for an artifact id, or (None, None)."""
    row = AR.identity_row_for(artifact_id, db=db)
    if row is None:
        return None, None
    ref = {"integration": row["integration"], "type": row["type"],
           "external_id": row["external_id"]}
    owner = AR.register_or_resolve(ref, db=db).owner_thread
    return ref, owner


def _settle_expiry(watch_id: str, reason: str, db) -> None:
    """Cancel this watch's unclaimed expiry continuation in the same write."""
    from automation.continuation.triggers import cancel_for_watch

    cancel_for_watch(watch_id, db=db, now=_now(), reason=reason)


def arm_watch(
    artifact_id: str,
    condition: dict,
    *,
    owner_thread: str | None = None,
    expected_event: str | None = None,
    plan_id: str | None = None,
    plan_node_id: str | None = None,
    armed_by_actor_id: str | None = None,
    db=None,
) -> Watch:
    """Arm (idempotently) an expected-event watch for an artifact + condition.

    The deadline condition is normalized to canonical UTC ISO and validated
    (tz-aware, parseable) before arming — via the single defer-condition contract
    — so stored deadlines never compare as raw local strings.

    Only an *armed* watch dedupes, and it dedupes as one thread's wait on one
    artifact's event: the artifact's full identity (an external id alone is
    shared across integrations), the owner (another thread's wait on the same
    event is its own, ending only when the event reaches that thread), the
    condition and the event. A resolved or expired watch is history: a second
    CI run on the same artifact is a new wait, not the terminal row of the first.

    Only a deadline ends by itself; every other watch ends when its artifact's
    event arrives. A non-deadline watch on no registered artifact could never
    end, so it is refused here, where every watch is armed (#1707).
    """
    if not isinstance(condition, dict) or not condition.get("type"):
        raise WatchError("watch requires a concrete condition with a 'type'")
    from registry.attention import AttentionError as _AErr, validate_defer_condition
    try:
        condition = validate_defer_condition(condition)
    except _AErr as exc:
        raise WatchError(str(exc)) from exc
    with write_transaction(db, scope="watch_arm") as _db:
        ref, resolved_owner = _resolve_artifact(artifact_id, _db)
        if ref is None and condition["type"] != "deadline":
            raise UnboundWatchError(UNBOUND_REFUSED)
        owner = owner_thread or resolved_owner
        identity = (
            (ref["integration"], ref["type"], ref["external_id"]) if ref
            else (None, None, artifact_id)
        )
        existing = _db.execute(
            "SELECT * FROM expected_event_watches "
            "WHERE artifact_integration IS ? AND artifact_type IS ? "
            "AND artifact_external_id=? AND owner_thread_id IS ? "
            "AND condition_type=? AND condition_value IS ? AND expected_event IS ? "
            "AND status='armed'",
            (*identity, owner, condition["type"], condition.get("value"), expected_event),
        ).fetchone()
        if existing is not None:
            watch = _row_to_watch(dict(existing))
            expected_binding = (
                plan_id, plan_node_id, owner, armed_by_actor_id,
            )
            actual_binding = (
                watch.plan_id, watch.plan_node_id,
                watch.owner_thread, watch.armed_by_actor_id,
            )
            if any(expected_binding) and actual_binding != expected_binding:
                raise WatchError("watch is bound to a different plan actor")
            return watch

        now = _now()
        wid = str(uuid.uuid4())[:12]
        _db.execute(
            """INSERT INTO expected_event_watches
               (id, artifact_integration, artifact_type, artifact_external_id,
                owner_thread_id, plan_id, plan_node_id, armed_by_actor_id,
                expected_event, condition_type, condition_value,
                status, armed_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'armed', ?,?,?)""",
            (wid, *identity, owner, plan_id, plan_node_id, armed_by_actor_id,
             expected_event, condition["type"], condition.get("value"), now, now, now))
        return _row_to_watch(dict(_db.execute(
            "SELECT * FROM expected_event_watches WHERE id=?", (wid,)).fetchone()))


def get_watch(watch_id: str, *, db=None) -> Watch:
    _db = db or get_db()
    try:
        row = _db.execute(
            "SELECT * FROM expected_event_watches WHERE id=?", (watch_id,)).fetchone()
        if row is None:
            raise WatchError(f"no watch {watch_id!r}")
        return _row_to_watch(dict(row))
    finally:
        if not db:
            _db.close()


def resolve_watch(
    watch_id: str,
    *,
    plan_id: str | None = None,
    plan_node_id: str | None = None,
    owner_thread: str | None = None,
    db=None,
) -> bool:
    """Resolve one watch idempotently after optional binding verification.

    Bound to where it waits, never who armed it: a wait armed by no one (#2969)
    is still its owner's."""

    with write_transaction(db, scope="watch_resolve") as _db:
        watch = get_watch(watch_id, db=_db)
        expected = (plan_id, plan_node_id, owner_thread)
        actual = (watch.plan_id, watch.plan_node_id, watch.owner_thread)
        if any(expected) and actual != expected:
            raise WatchError("watch is unavailable to the current plan actor")
        cur = _db.execute(
            "UPDATE expected_event_watches SET status='resolved', resolved_at=?, "
            "updated_at=? WHERE id=? AND status IN ('armed','expired')", (_now(), _now(), watch_id))
        if cur.rowcount == 1:
            _settle_expiry(watch_id, "watch_resolved", _db)
        return cur.rowcount == 1


def expire_watch(watch_id: str, *, db=None) -> ExpiryResult:
    """Expire an armed watch once and route the missing-event failure (S4b).

    A second expiry (or a restart replay) is a no-op: the armed→expired
    transition is gated, so it can never double-wake or double-clear.
    """
    with write_transaction(db, scope="watch_expire") as _db:
        watch = get_watch(watch_id, db=_db)
        if watch.status == RESOLVED:
            return ExpiryResult(watch_id, routed_wakes=0, resolution=None, already_final=True)
        # Resolve and claim against the same declared database owner.
        target = resolve_delivery_failure(
            watch.owner_thread, reason="expected_event_missing", db=_db)

        # ATOMIC CLAIM: ``routed_at IS NULL`` is the set-once guard. One UPDATE
        # both expires and claims the routing, so concurrent expiry matches one
        # row (no double-wake) and a prior crash that left it expired-but-unrouted
        # re-routes. The wake + re-raise share this transaction, so a crash before
        # commit rolls the claim back and a retry routes exactly once.
        cur = _db.execute(
            "UPDATE expected_event_watches "
            "SET status='expired', expired_at=COALESCE(expired_at, ?), "
            "    routed_at=?, updated_at=? "
            "WHERE id=? AND routed_at IS NULL AND status IN ('armed','expired')",
            (_now(), _now(), _now(), watch_id))
        if cur.rowcount != 1:
            return ExpiryResult(watch_id, routed_wakes=0, resolution=None, already_final=True)

        _settle_expiry(watch_id, "watch_expired", _db)
        # The deadline passed with no plan write: tell the plan's readers.
        from plans.public import schedule_after_commit
        schedule_after_commit([watch.plan_id], db=_db)
        artifact_present = _route_failure_wake(watch, target, _db)
        ref = watch.artifact_ref
        if artifact_present and ref and ref["integration"] is not None:
            _maybe_reraise(ref, _db)
        # Routed either way, to the owner/health sink or the dead-letter sink.
        resolution = target.resolution if artifact_present else "dead_letter"
        result = ExpiryResult(watch_id, routed_wakes=1, resolution=resolution,
                              target=target.thread)
    event = expiry_event(result)
    if event is not None and (db is None or not db.in_transaction):
        category, action, properties = event
        emit_product_event(category, action, properties=properties)
    return result


def _route_failure_wake(watch: Watch, target, db) -> bool:
    """Record the routed delivery_failure wake. Returns False (and parks the
    failure to the dead-letter health sink, LOUD) when the artifact identity has
    vanished since the claim, so a missing artifact is never a silent dropped
    wake. The journal shares the claim transaction (exactly-once).
    """
    ref = watch.artifact_ref
    if ref is None or ref["integration"] is None:
        return True  # a plan-node watch: the T2 source wakes its owner
    row = db.execute(
        "SELECT id FROM artifact_identities WHERE integration=? AND type=? "
        "AND external_id=?", (ref["integration"], ref["type"], ref["external_id"])).fetchone()
    if row is None:
        from events import dead_letter
        dead_letter.park(
            "automation.watch_expired",
            {"watch_id": watch.id, "artifact_ref": ref,
             "target": target.thread, "resolution": target.resolution},
            "artifact_identity_missing_at_expiry",
            ref["external_id"], db=db)
        return False
    from registry import models
    models.record_artifact_change(
        artifact_id=row["id"],
        internal_event="automation.watch_expired",
        external_id=ref["external_id"],
        summary="expected event missing — watch expired",
        provenance={"resolution": target.resolution,
                    "disposition": target.disposition, "watch_id": watch.id},
        db=db)
    return True


def _maybe_reraise(artifact_ref: dict, db) -> None:
    """Re-raise a deferred item atomically with the caller's expiry wake."""
    state = attention.get_attention_state(artifact_ref["external_id"], db=db)
    if state == attention.ATTENTION_DEFERRED:
        from registry import models
        models.update_metadata(
            artifact_ref["integration"], artifact_ref["type"],
            artifact_ref["external_id"],
            {"attention_state": attention.ATTENTION_RAISED}, db=db)


def _parse_utc(value: str) -> datetime:
    """Parse a deadline through the canonical defer-condition validator."""
    condition = attention.validate_defer_condition({"type": "deadline", "value": value})
    return datetime.fromisoformat(condition["value"])


def due_watches(now_iso: str, *, db=None) -> list[Watch]:
    """Armed deadline watches whose deadline is at/past ``now_iso``.

    Compares parsed datetimes, not raw strings, so mixed ISO formats can't cause
    a lexicographic misorder. Deadlines are stored canonical (arm-time
    normalization); a non-parseable legacy value is skipped, never silently 'due'.
    """
    now_dt = _parse_utc(now_iso)
    _db = db or get_db()
    try:
        rows = _db.execute(
            """SELECT w.* FROM expected_event_watches w
               WHERE w.status='armed' AND w.condition_type='deadline'
                 AND (
                   w.plan_id IS NULL OR EXISTS (
                     SELECT 1 FROM plans p
                     WHERE p.id=w.plan_id AND p.state='active'
                   )
                 )"""
        ).fetchall()
        due: list[Watch] = []
        for r in rows:
            try:
                if _parse_utc(r["condition_value"]) <= now_dt:
                    due.append(_row_to_watch(dict(r)))
            except (attention.AttentionError, ValueError, TypeError):
                continue  # malformed legacy value — not silently treated as due
        return due
    finally:
        if not db:
            _db.close()


def sweep_expired(now_iso: str, *, db=None) -> int:
    """Expire every due deadline watch; return the count of routed failures."""
    _db = db if db is not None else get_db()
    if db is None and _db.in_transaction:
        raise TransactionOwnershipError("watch sweep requires a clean anonymous connection")
    try:
        routed = 0
        for watch in due_watches(now_iso, db=_db):
            routed += expire_watch(watch.id, db=_db).routed_wakes
        return routed
    finally:
        if not db:
            _db.close()


def rearm_on_startup(*, db=None) -> dict[str, Any]:
    """Re-arm persisted watches after a runtime restart.

    Persisted ``armed`` watches survive a restart (re-arm = confirm + surface);
    a watch whose artifact has vanished is flagged loud, never silently dropped.
    """
    _db = db or get_db()
    try:
        rows = [dict(r) for r in _db.execute(
            "SELECT * FROM expected_event_watches WHERE status='armed'").fetchall()]
        orphaned = []
        for row in rows:
            ref, _ = _resolve_artifact(row["artifact_external_id"], _db)
            if ref is None:
                orphaned.append(row["id"])
        return {"rearmed": len(rows) - len(orphaned), "orphaned": orphaned}
    finally:
        if not db:
            _db.close()
