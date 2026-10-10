"""Turn-owned pending receipts for idempotent LLM usage projection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from loguru import logger

from infra.database import get_db, write_transaction
from infra.llm_usage_events import emit_llm_usage_recorded


from infra.usage_row import MEASURED_FIELDS, PLAN_FIELDS, write_usage_row

# One pass can recover a full maximum-length Native Turn (200 provider calls)
# plus headroom without making service startup proportional to retained history.
STARTUP_USAGE_RECEIPT_LIMIT = 256


class UsageReceiptError(RuntimeError):
    """A Turn-owned usage handoff could not complete."""


class UsageReceiptConflictError(UsageReceiptError):
    """One stable Turn usage key was reused for different measurement truth."""


@dataclass(frozen=True)
class UsageProjection:
    """One consumed receipt and whether it created a canonical ledger row."""

    inserted: bool
    thread_id: str
    model: str
    measured: Mapping[str, Any]
    ttft_ms: int | None
    source: str



def _normalized_measurement(measured: Mapping[str, Any]) -> dict[str, Any]:
    """Keep canonical nullable provider fields without inventing zeros."""
    return {field: measured.get(field) for field in MEASURED_FIELDS}


def _receipt_id(turn_id: str, usage_key: str) -> str:
    material = f"turn-usage\0{turn_id}\0{usage_key}".encode()
    return hashlib.sha256(material).hexdigest()[:24]


def _receipt_payload(
    *,
    thread_id: str,
    turn_id: str | None,
    usage_key: str,
    model: str,
    measured: Mapping[str, Any],
    ttft_ms: int | None,
    binding: Mapping[str, Any],
    source: str,
    now_iso: str,
    context_manifest_id: str | None = None,
    vendor_account_id: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    # ``context_manifest_id`` names the guidance the Turn delivered; like the
    # plan binding, it is attribution, not measurement, so replays ignore it.
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "usage_key": usage_key,
        "model": model,
        "measured": _normalized_measurement(measured),
        "ttft_ms": ttft_ms,
        "binding": {field: binding[field] for field in PLAN_FIELDS},
        "source": source,
        "created_at": now_iso,
        "context_manifest_id": context_manifest_id,
        "vendor_account_id": vendor_account_id,
        "reasoning_effort": reasoning_effort,
    }


def _same_receipt_truth(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Compare paid account truth and measurements, excluding observation time."""
    fields = ("thread_id", "turn_id", "usage_key", "model", "ttft_ms", "source", "vendor_account_id")
    return all(left.get(field) == right.get(field) for field in fields) and (
        left.get("measured") == right.get("measured")
    )


def _projection_measurement_matches(row: Any, payload: Mapping[str, Any]) -> bool:
    measured = payload["measured"]
    values = {
        "thread_id": payload["thread_id"],
        "turn_id": payload["turn_id"],
        "model": payload["model"],
        **{field: measured[field] for field in MEASURED_FIELDS},
        "ttft_ms": payload["ttft_ms"],
        "source": payload["source"],
        "vendor_account_id": payload.get("vendor_account_id"),
    }
    return all(row[field] == value for field, value in values.items())


def _projection_matches(row: Any, payload: Mapping[str, Any]) -> bool:
    return _projection_measurement_matches(row, payload) and all(
        row[field] == payload["binding"][field] for field in PLAN_FIELDS
    )


def _decode_receipts(raw: str) -> dict[str, Any]:
    receipts = json.loads(raw)
    if not isinstance(receipts, dict):
        raise UsageReceiptConflictError("Turn usage receipts are not an object")
    return receipts


def store_turn_usage_receipt(
    db: Any,
    *,
    thread_id: str,
    turn_id: str,
    usage_key: str,
    model: str,
    measured: Mapping[str, Any],
    ttft_ms: int | None,
    source: str,
    now_iso: str,
    binding: Mapping[str, Any] | None = None,
    reasoning_effort: str | None = None,
) -> tuple[str, bool, Mapping[str, Any]] | None:
    """Commit one pending receipt before its independently failing projection.

    ``binding`` is the call's own attribution; absent, the Turn's snapshot.
    The guidance list is the Turn's at store time. Native stores each call's
    receipt before its next call can adopt a checkpoint's new list."""
    if not usage_key.strip():
        raise ValueError("turn usage_key must be non-empty")
    identity = _receipt_id(turn_id, usage_key)
    with write_transaction(db, scope="store_turn_usage_receipt") as tx:
        turn = tx.execute(
            """SELECT usage_receipts_json,plan_id,plan_node_id,plan_attempt_id,
                      plan_commitment_id,plan_usage_role,context_manifest_id,vendor_account_id
               FROM active_turns WHERE id=? AND thread_id=?""",
            (turn_id, thread_id),
        ).fetchone()
        if turn is None:
            return None
        attribution = binding if binding is not None else turn
        binding = {field: attribution[field] for field in PLAN_FIELDS}
        payload = _receipt_payload(
            thread_id=thread_id,
            turn_id=turn_id,
            usage_key=usage_key,
            model=model,
            measured=measured,
            ttft_ms=ttft_ms,
            binding=binding,
            source=source,
            now_iso=now_iso,
            context_manifest_id=turn["context_manifest_id"],
            vendor_account_id=turn["vendor_account_id"],
            reasoning_effort=reasoning_effort,
        )
        projected = tx.execute(
            "SELECT * FROM llm_usage WHERE id=?", (identity,)
        ).fetchone()
        if projected is not None:
            if not _projection_measurement_matches(projected, payload):
                raise UsageReceiptConflictError(
                    f"usage key {usage_key!r} has a different measurement"
                )
            return identity, True, binding
        receipts = _decode_receipts(str(turn["usage_receipts_json"]))
        existing = receipts.get(identity)
        if existing is not None and not _same_receipt_truth(existing, payload):
            raise UsageReceiptConflictError(
                f"usage key {usage_key!r} has a different measurement"
            )
        if existing is None:
            receipts[identity] = payload
            tx.execute(
                "UPDATE active_turns SET usage_receipts_json=? WHERE id=?",
                (json.dumps(receipts, sort_keys=True, separators=(",", ":")), turn_id),
            )
        return identity, False, binding


def _project_turn_usage_receipt_in_transaction(
    db: Any, turn_id: str, identity: str,
) -> UsageProjection | None:
    """Project and consume one receipt inside the caller's transaction."""
    turn = db.execute(
        "SELECT usage_receipts_json FROM active_turns WHERE id=?",
        (turn_id,),
    ).fetchone()
    if turn is None:
        return None
    receipts = _decode_receipts(str(turn["usage_receipts_json"]))
    payload = receipts.get(identity)
    if payload is None:
        return None
    binding = payload["binding"]
    existing = db.execute(
        "SELECT * FROM llm_usage WHERE id=?", (identity,)
    ).fetchone()
    inserted = existing is None
    if inserted:
        write_usage_row(
            db,
            usage_id=identity,
            thread_id=payload["thread_id"],
            turn_id=payload["turn_id"],
            model=payload["model"],
            measured=payload["measured"],
            ttft_ms=payload["ttft_ms"],
            binding=binding,
            source=payload["source"],
            now_iso=payload["created_at"],
            context_manifest_id=payload.get("context_manifest_id"),
            vendor_account_id=payload.get("vendor_account_id"),
            reasoning_effort=payload.get("reasoning_effort"),
        )
    elif not _projection_matches(existing, payload):
        raise UsageReceiptConflictError(
            f"receipt {identity!r} conflicts with its ledger projection"
        )
    del receipts[identity]
    db.execute(
        "UPDATE active_turns SET usage_receipts_json=? WHERE id=?",
        (json.dumps(receipts, sort_keys=True, separators=(",", ":")), turn_id),
    )
    return UsageProjection(
        inserted=inserted,
        thread_id=str(payload["thread_id"]),
        model=str(payload["model"]),
        measured=payload["measured"],
        ttft_ms=payload["ttft_ms"],
        source=str(payload["source"]),
    )


def project_turn_usage_receipt(
    db: Any, turn_id: str, identity: str,
) -> UsageProjection | None:
    """Project and consume one receipt atomically after its durable handoff."""
    with write_transaction(db, scope="project_turn_usage_receipt") as tx:
        return _project_turn_usage_receipt_in_transaction(tx, turn_id, identity)


def attribute_unbound_turn_usage(
    db: Any,
    *,
    thread_id: str,
    turn_id: str,
    binding: Mapping[str, Any],
) -> None:
    """Attribute a Turn's earlier unbound usage to the binding it just took.

    Called once, right after a mid-Turn binding is written (selection or review
    verification), so the bound attempt carries the whole Turn's cost.
    """
    db.execute(
        """UPDATE llm_usage
           SET plan_id=?,plan_node_id=?,plan_attempt_id=?,
               plan_commitment_id=?,plan_usage_role=?
           WHERE turn_id=? AND thread_id=? AND plan_id IS NULL""",
        (*(binding[field] for field in PLAN_FIELDS), turn_id, thread_id),
    )
    row = db.execute(
        """SELECT usage_receipts_json FROM active_turns
           WHERE id=? AND thread_id=?""",
        (turn_id, thread_id),
    ).fetchone()
    if row is None:
        return
    receipts = _decode_receipts(str(row["usage_receipts_json"]))
    changed = False
    for payload in receipts.values():
        if all(payload["binding"][field] is None for field in PLAN_FIELDS):
            payload["binding"] = {field: binding[field] for field in PLAN_FIELDS}
            changed = True
    if changed:
        db.execute(
            "UPDATE active_turns SET usage_receipts_json=? WHERE id=?",
            (json.dumps(receipts, sort_keys=True, separators=(",", ":")), turn_id),
        )


def project_thread_usage_before_turn_deletion(db: Any, thread_id: str) -> int:
    """Consume a thread's pending receipts before its Turn audit is destroyed."""
    pending = [
        (str(row["id"]), tuple(_decode_receipts(row["usage_receipts_json"])))
        for row in db.execute(
            """SELECT id,usage_receipts_json FROM active_turns
               WHERE thread_id=? AND usage_receipts_json <> '{}' ORDER BY turn_order""",
            (thread_id,),
        ).fetchall()
    ]
    projected = 0
    for turn_id, identities in pending:
        for identity in identities:
            projection = _project_turn_usage_receipt_in_transaction(
                db, turn_id, identity
            )
            projected += int(projection is not None and projection.inserted)
    return projected


def reconcile_pending_turn_usage(db: Any | None = None) -> int:
    """Run one bounded startup pass over durable pending Turn receipts."""
    own_connection = db is None
    connection = db or get_db()
    try:
        pending = [
            (str(row["turn_id"]), str(row["receipt_id"]))
            for row in connection.execute(
                """SELECT turns.id AS turn_id, receipts.key AS receipt_id
                   FROM active_turns AS turns,
                        json_each(turns.usage_receipts_json) AS receipts
                   WHERE turns.usage_receipts_json <> '{}'
                   ORDER BY turns.turn_order,receipts.key
                   LIMIT ?""",
                (STARTUP_USAGE_RECEIPT_LIMIT,),
            ).fetchall()
        ]
        projected = 0
        for turn_id, identity in pending:
            try:
                projection = project_turn_usage_receipt(connection, turn_id, identity)
                if projection is not None and projection.inserted:
                    projected += 1
                    emit_llm_usage_recorded(
                        projection.thread_id,
                        projection.model,
                        projection.measured,
                        source=projection.source,
                        ttft_ms=projection.ttft_ms,
                    )
            except Exception:
                logger.exception(
                    "llm_usage: retained pending receipt {} for next startup",
                    identity,
                )
        if len(pending) == STARTUP_USAGE_RECEIPT_LIMIT:
            logger.warning(
                "llm_usage: startup reconciliation reached receipt limit {}; "
                "any remaining receipts are retained for a later startup",
                STARTUP_USAGE_RECEIPT_LIMIT,
            )
        return projected
    finally:
        if own_connection:
            connection.close()
