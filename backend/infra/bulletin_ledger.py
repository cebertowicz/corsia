"""The sole durable owner for System-bulletin ledger state.

Transition matrix:
* new -> pending or skipped; skipped -> pending when a bulletin becomes applicable;
* pending -> delivered after a successful dispatch, unless the conductor records a
  disposition during that dispatch;
* pending, delivered, acted, not_applicable, or deferred -> a conductor disposition;
* pending or skipped -> withdrawn silently; delivered, acted, not_applicable, or
  deferred -> withdrawn after its notice dispatches; withdrawn is terminal.
* a pending System change is superseded to skipped when a newer identity is current;
  a skipped System change may be claimed again if that identity returns, and an
  acted or not_applicable one when the baseline has since moved to another identity
  (a rollback to an already-acknowledged System).
* a System change seen before any Turn has run becomes the baseline with no notice.

Every transition is guarded by the prior state. A rejected transition is logged
and reported to the caller instead of being silently treated as success.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger


_CONDUCTOR_STATES = {"acted", "not_applicable", "deferred"}
_NOTICE_WITHDRAWAL_STATES = {"delivered", "acted", "not_applicable", "deferred"}
_SILENT_WITHDRAWAL_STATES = {"pending", "skipped"}


def _refused(action: str, bulletin_id: str, state: str | None) -> bool:
    logger.warning("refused bulletin ledger transition {} for {} from {}", action, bulletin_id, state)
    return False


def _row(db, bulletin_id: str):
    return db.execute(
        "SELECT source,state,disposition,delivery_id FROM system_bulletins WHERE id=?", (bulletin_id,),
    ).fetchone()


def delivery_state(db, bulletin_id: str) -> str | None:
    """Return the durable state for one bulletin without exposing a SQL owner."""
    row = _row(db, bulletin_id)
    return row["state"] if row is not None else None


def bulletin_source(db, bulletin_id: str) -> str | None:
    """Return a ledger row's source without exposing direct table access."""
    row = _row(db, bulletin_id)
    return row["source"] if row is not None else None


def delivery_id(db, bulletin_id: str) -> str | None:
    """Return the persisted idempotency key for a claimed delivery."""
    row = _row(db, bulletin_id)
    return row["delivery_id"] if row is not None else None


def claim_delivery(db, bulletin_id: str, source: str, *, delivery_id: str | None = None) -> bool:
    """Claim a new or newly applicable bulletin before attempting dispatch."""
    from infra.bulletins import _now

    row = _row(db, bulletin_id)
    stamp = _now()
    if row is None:
        db.execute(
            "INSERT INTO system_bulletins(id,source,state,delivery_id,delivered_at,updated_at) VALUES(?,?,?,?,?,?)",
            (bulletin_id, source, "pending", delivery_id, None, stamp),
        )
        return True
    if row["source"] != source:
        return _refused("claim", bulletin_id, row["state"])
    if row["state"] == "pending":
        if delivery_id is not None and row["delivery_id"] is None:
            db.execute(
                "UPDATE system_bulletins SET delivery_id=?,updated_at=? WHERE id=? AND state='pending' AND delivery_id IS NULL",
                (delivery_id, stamp, bulletin_id),
            )
        return True
    if row["state"] != "skipped":
        return _refused("claim", bulletin_id, row["state"])
    changed = db.execute(
        "UPDATE system_bulletins SET state='pending',disposition=NULL,delivery_id=?,delivered_at=NULL,updated_at=? "
        "WHERE id=? AND source=? AND state='skipped'",
        (delivery_id, stamp, bulletin_id, source),
    ).rowcount
    return bool(changed) or _refused("claim", bulletin_id, "skipped")


def mark_skipped(db, bulletin_id: str, source: str, disposition: str) -> bool:
    """Record a newly inapplicable bulletin without disturbing a terminal row."""
    from infra.bulletins import _now

    row = _row(db, bulletin_id)
    stamp = _now()
    if row is None:
        db.execute(
            "INSERT INTO system_bulletins(id,source,state,disposition,delivered_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (bulletin_id, source, "skipped", disposition, None, stamp),
        )
        return True
    if row["source"] != source or row["state"] != "skipped":
        return _refused("skip", bulletin_id, row["state"])
    changed = db.execute(
        "UPDATE system_bulletins SET disposition=?,updated_at=? "
        "WHERE id=? AND source=? AND state='skipped'",
        (disposition, stamp, bulletin_id, source),
    ).rowcount
    return bool(changed) or _refused("skip", bulletin_id, "skipped")


def complete_delivery(db, bulletin_id: str, source: str) -> bool:
    """Finish only a pending claim after dispatch accepts its deterministic key."""
    from infra.bulletins import _now

    stamp = _now()
    changed = db.execute(
        "UPDATE system_bulletins SET state='delivered',delivered_at=?,updated_at=? "
        "WHERE id=? AND source=? AND state='pending'",
        (stamp, stamp, bulletin_id, source),
    ).rowcount
    if changed:
        return True
    return _refused("complete delivery", bulletin_id, delivery_state(db, bulletin_id))


def withdrawal_action(db, bulletin_id: str, source: str) -> str | None:
    """Classify a withdrawal as silent, notice-bearing, or already terminal."""
    row = _row(db, bulletin_id)
    if row is None:
        if not claim_delivery(db, bulletin_id, source):
            return None
        return "silent"
    if row["source"] != source:
        _refused("withdraw", bulletin_id, row["state"])
        return None
    if row["state"] in _SILENT_WITHDRAWAL_STATES:
        return "silent"
    if row["state"] in _NOTICE_WITHDRAWAL_STATES:
        return "notice"
    if row["state"] == "withdrawn":
        return None
    _refused("withdraw", bulletin_id, row["state"])
    return None


def complete_withdrawal(db, bulletin_id: str, source: str, *, after_notice: bool) -> bool:
    """Make a withdrawal terminal after its required delivery outcome."""
    from infra.bulletins import _now

    allowed = _NOTICE_WITHDRAWAL_STATES if after_notice else _SILENT_WITHDRAWAL_STATES
    placeholders = ",".join("?" for _ in allowed)
    stamp = _now()
    changed = db.execute(
        "UPDATE system_bulletins SET state='withdrawn',withdrawn_at=?,updated_at=? "
        f"WHERE id=? AND source=? AND state IN ({placeholders})",
        (stamp, stamp, bulletin_id, source, *sorted(allowed)),
    ).rowcount
    if changed:
        return True
    return _refused("complete withdrawal", bulletin_id, delivery_state(db, bulletin_id))


def record_disposition(db, bulletin_id: str, state: str, disposition: str) -> bool:
    """Apply the conductor's disposition while preserving a terminal withdrawal."""
    from infra.bulletins import _now

    if state not in _CONDUCTOR_STATES:
        return _refused("disposition", bulletin_id, state)
    row = _row(db, bulletin_id)
    if row is None:
        return _refused("disposition", bulletin_id, None)
    stamp = _now()
    acted_at = stamp if state == "acted" else None
    if row["state"] == "withdrawn":
        changed = db.execute(
            "UPDATE system_bulletins SET disposition=?,acted_at=?,updated_at=? "
            "WHERE id=? AND state='withdrawn'",
            (disposition, acted_at, stamp, bulletin_id),
        ).rowcount
    elif row["state"] in {"pending", "delivered", *(_CONDUCTOR_STATES)} or (
        row["source"] == "system_change" and row["state"] == "skipped"
    ):
        changed = db.execute(
            "UPDATE system_bulletins SET state=?,disposition=?,acted_at=?,updated_at=? "
            "WHERE id=? AND (state IN ('pending','delivered','acted','not_applicable','deferred') "
            "OR (source='system_change' AND state='skipped'))",
            (state, disposition, acted_at, stamp, bulletin_id),
        ).rowcount
    else:
        return _refused("disposition", bulletin_id, row["state"])
    return bool(changed) or _refused("disposition", bulletin_id, row["state"])


def pending_system_change(db) -> dict[str, Any] | None:
    """Persist and return the one undelivered System-change notice, if any."""
    from infra import bulletins

    current = bulletins.system_identity()
    row = db.execute(
        "SELECT version,digest,files_json,reconciled_at FROM system_bulletin_identity WHERE id=1",
    ).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO system_bulletin_identity(id,version,digest,files_json,reconciled_at) VALUES(1,?,?,?,?)",
            (current.version, current.digest, json.dumps(current.files), bulletins._now()),
        )
        return None
    if row["digest"] == current.digest and row["reconciled_at"] is not None:
        _supersede_pending_system_changes(db, None)
        return None
    if db.execute("SELECT 1 FROM active_turns LIMIT 1").fetchone() is None:
        # No Turn has run, so no agent was ever served the old System and nothing
        # can disagree with the current one: it becomes the baseline, unannounced.
        moved = db.execute(
            "UPDATE system_bulletin_identity SET version=?,digest=?,files_json=?,reconciled_at=? "
            "WHERE id=1 AND digest=? AND version IS ?",
            (current.version, current.digest, json.dumps(current.files), bulletins._now(),
             row["digest"], row["version"]),
        ).rowcount
        if not moved:
            _refused("baseline", "system-change:" + current.digest, row["digest"])
            return None
        _supersede_pending_system_changes(db, None)
        return None
    change = {
        "id": "system-change:" + current.digest,
        "old_version": row["version"], "old_digest": row["digest"],
        "new_version": current.version, "new_digest": current.digest,
        "changes": bulletins._identity_changes(row["files_json"], current),
    }
    _supersede_pending_system_changes(db, change["id"])
    state = delivery_state(db, change["id"])
    if state in {"acted", "not_applicable"} and row["digest"] != current.digest:
        # The baseline moved on after this identity was acknowledged: a rollback.
        state = "pending" if _reclaim_system_change(db, change["id"]) else state
    elif state in {None, "skipped"}:
        claim_delivery(db, change["id"], "system_change")
        state = "pending"
    return change if state == "pending" else None


def _reclaim_system_change(db, change_id: str) -> bool:
    """Reopen an acknowledged System change so a rollback to it is announced again."""
    from infra.bulletins import _now

    changed = db.execute(
        "UPDATE system_bulletins SET state='pending',disposition=NULL,acted_at=NULL,delivery_id=NULL,"
        "delivered_at=NULL,updated_at=? WHERE id=? AND source='system_change' "
        "AND state IN ('acted','not_applicable')",
        (_now(), change_id),
    ).rowcount
    return bool(changed) or _refused("reclaim", change_id, delivery_state(db, change_id))


def has_pending_system_change(db) -> bool:
    """Return whether the current System-change delivery awaits retry."""
    from infra import bulletins

    current_id = "system-change:" + bulletins.system_identity().digest
    return db.execute(
        "SELECT 1 FROM system_bulletins WHERE id=? AND source='system_change' AND state='pending'",
        (current_id,),
    ).fetchone() is not None


def _supersede_pending_system_changes(db, current_id: str | None) -> None:
    """Retire pending notices for identities that can no longer be reconciled."""
    from infra.bulletins import _now

    disposition = (
        f"superseded by {current_id}" if current_id is not None
        else "superseded: System reconciled"
    )
    query = (
        "UPDATE system_bulletins SET state='skipped',disposition=?,updated_at=? "
        "WHERE source='system_change' AND state='pending'"
    )
    parameters: tuple[object, ...] = (disposition, _now())
    if current_id is not None:
        query += " AND id<>?"
        parameters += (current_id,)
    db.execute(query, parameters)


def mark_system_reconciled(db, change_id: str) -> bool:
    """Persist the baseline only when the acknowledged change is still current."""
    from infra import bulletins

    current = bulletins.system_identity()
    if change_id != "system-change:" + current.digest:
        return False
    changed = db.execute(
        "UPDATE system_bulletin_identity SET version=?,digest=?,files_json=?,reconciled_at=? WHERE id=1",
        (current.version, current.digest, json.dumps(current.files), bulletins._now()),
    ).rowcount
    return bool(changed)
