"""Durable idempotency receipts for actor-bound structured-plan mutations."""

from __future__ import annotations

import hashlib
import json

from plans import service


def request_fingerprint(operation: str, params: dict) -> str:
    """Hash one canonical public mutation request without actor identity."""
    payload = json.dumps(
        {"operation": operation, "params": params},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load(
    connection,
    *,
    plan_id: str,
    mutation_id: str,
    actor_id: str,
    operation: str,
    fingerprint: str,
) -> dict | None:
    """Return the recorded result or reject a reused idempotency key."""
    receipt = connection.execute(
        "SELECT actor_id, operation, request_fingerprint, result_json "
        "FROM plan_mutation_receipts WHERE plan_id=? AND mutation_id=?",
        (plan_id, mutation_id),
    ).fetchone()
    if receipt is None:
        return None
    if (
        receipt["actor_id"] != actor_id
        or receipt["operation"] != operation
        or receipt["request_fingerprint"] != fingerprint
    ):
        raise service.PlanError("mutation_id is already bound to a different request")
    return json.loads(receipt["result_json"])


def record(
    connection,
    *,
    plan_id: str,
    mutation_id: str,
    actor_id: str,
    operation: str,
    fingerprint: str,
    result: dict,
) -> None:
    """Persist one successful mutation result inside the surrounding transaction."""
    connection.execute(
        "INSERT INTO plan_mutation_receipts "
        "(plan_id,mutation_id,actor_id,operation,request_fingerprint,result_json,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            plan_id,
            mutation_id,
            actor_id,
            operation,
            fingerprint,
            json.dumps(result, sort_keys=True, separators=(",", ":")),
            service._now(),
        ),
    )
