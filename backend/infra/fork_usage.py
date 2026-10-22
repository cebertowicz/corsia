"""Ledger receipts for disposable forks without a checkpoint row."""

from __future__ import annotations

import hashlib

from infra.database import write_transaction
from infra.turn_usage_receipts import (
    PLAN_FIELDS, UsageReceiptConflictError, _projection_matches,
    _receipt_payload, write_usage_row,
)


def record_fork_call(fork_id, thread_id, call_key, model, measured, *, vendor_account_id, now_iso, source, db=None):
    """Record fork spend on its reviewer thread without a plan binding.

    Per-request attribution awaits a later Structured Plans read. The ledger
    row is the receipt, retained once per provider call, including failures.
    """
    if not isinstance(call_key, str) or not call_key.strip():
        raise ValueError("fork call key must be non-empty")
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
