"""Checkpoint-owned usage receipts and ledger rows in one writer transaction."""

from __future__ import annotations

import hashlib
import json

from infra.database import write_transaction
from infra.turn_usage_receipts import (
    PLAN_FIELDS, UsageReceiptConflictError, _projection_matches,
    _receipt_payload, _same_receipt_truth, write_usage_row,
)


def _read(db, checkpoint_id):
    row = db.execute("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
    if row is None:
        raise UsageReceiptConflictError("checkpoint usage owner does not exist")
    payload = json.loads(row["usage_json"]) if row["usage_json"] else {}
    if not isinstance(payload, dict):
        raise UsageReceiptConflictError("checkpoint usage is not an object")
    return row, payload


def _write(db, checkpoint_id, payload):
    db.execute("UPDATE checkpoints SET usage_json=? WHERE id=?", (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False), checkpoint_id,
    ))


def freeze_binding(checkpoint_id, binding, *, vendor_account_id=None, db=None):
    """Freeze dispatch-time task and ACTUAL writer account, independently of source Turn."""
    frozen = {field: binding.get(field) if binding else None for field in PLAN_FIELDS}
    with write_transaction(db, scope="checkpoint_usage_binding") as connection:
        row, payload = _read(connection, checkpoint_id)
        if "binding" in payload:
            if payload["binding"] != frozen or payload.get("vendor_account_id") != vendor_account_id:
                raise UsageReceiptConflictError("checkpoint attribution is already frozen")
            return
        payload.update(binding=frozen, originating_turn_id=row["originating_turn_id"],
                       vendor_account_id=vendor_account_id)
        _write(connection, checkpoint_id, payload)


def record_call(checkpoint_id, call_key, model, measured, *, now_iso, db=None):
    """Commit one paid observation once, even after failure or cancellation.

    The ledger Turn stays NULL: provenance is on the checkpoint receipt, so it
    cannot suppress an ordinary Turn's still-required terminal usage fallback.
    """
    if not isinstance(call_key, str) or not call_key.strip():
        raise ValueError("checkpoint call key must be non-empty")
    identity = hashlib.sha256(f"checkpoint-usage\0{checkpoint_id}\0{call_key}".encode()).hexdigest()[:24]
    with write_transaction(db, scope="checkpoint_usage_call") as connection:
        row, usage = _read(connection, checkpoint_id)
        if "binding" not in usage:
            raise UsageReceiptConflictError("checkpoint attribution was not frozen")
        payload = _receipt_payload(
            thread_id=row["thread_id"], turn_id=None, usage_key=call_key,
            model=model, measured=measured, ttft_ms=None, binding=usage["binding"],
            source="checkpoint", now_iso=now_iso,
            vendor_account_id=usage.get("vendor_account_id"),
        )
        receipts = usage.setdefault("receipts", {})
        previous = receipts.get(identity)
        if previous is not None and not _same_receipt_truth(previous, payload):
            raise UsageReceiptConflictError("checkpoint call changed its measurement")
        projected = connection.execute("SELECT * FROM llm_usage WHERE id=?", (identity,)).fetchone()
        if projected is not None:
            if not _projection_matches(projected, payload):
                raise UsageReceiptConflictError("checkpoint receipt conflicts with its ledger")
            return False
        write_usage_row(
            connection, usage_id=identity, thread_id=row["thread_id"], turn_id=None,
            model=model, measured=payload["measured"], ttft_ms=None,
            binding=usage["binding"], source="checkpoint", now_iso=now_iso,
            vendor_account_id=usage.get("vendor_account_id"),
        )
        receipts[identity] = payload
        usage["calls"] = [receipt["measured"] for receipt in receipts.values()]
        _write(connection, checkpoint_id, usage)
    return True


def record_fork_call(fork_id, thread_id, call_key, model, measured, *, vendor_account_id, now_iso, source, db=None):
    """Commit one observation of a fork with no checkpoint row, once per call.

    The ledger row is the receipt: the same per-call id and writer contract as
    a checkpoint call, since the same adapters measured it, and no plan
    attribution, since the fork serves its requester, not the thread's task.
    """
    if not isinstance(call_key, str) or not call_key.strip():
        raise ValueError("checkpoint call key must be non-empty")
    identity = hashlib.sha256(f"{source}-usage\0{fork_id}\0{call_key}".encode()).hexdigest()[:24]
    binding = dict.fromkeys(PLAN_FIELDS)
    payload = _receipt_payload(
        thread_id=thread_id, turn_id=None, usage_key=call_key, model=model, measured=measured,
        ttft_ms=None, binding=binding, source=source, now_iso=now_iso,
        vendor_account_id=vendor_account_id,
    )
    with write_transaction(db, scope="fork_usage_call") as connection:
        projected = connection.execute("SELECT * FROM llm_usage WHERE id=?", (identity,)).fetchone()
        if projected is not None:
            if not _projection_matches(projected, payload):
                raise UsageReceiptConflictError("fork call changed its measurement")
            return False
        write_usage_row(
            connection, usage_id=identity, thread_id=thread_id, turn_id=None, model=model,
            measured=payload["measured"], ttft_ms=None, binding=binding, source=source,
            now_iso=now_iso, vendor_account_id=vendor_account_id,
        )
    return True
