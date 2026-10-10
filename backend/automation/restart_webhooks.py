"""Held webhook claim, replay, retry, and terminal poison handling."""

from __future__ import annotations

import json
import uuid

from events.connectors.github_webhook import (
    WebhookDispatchPoison,
    dispatch_github_webhook_for_replay,
)
from infra.database import get_db, write_transaction

WebhookReplayPoison = WebhookDispatchPoison
_BOOT_OWNER_TOKEN = uuid.uuid4().hex


def held_webhook_count(*, db=None) -> int:
    connection = db or get_db()
    try:
        return int(connection.execute(
            "SELECT COUNT(*) FROM webhook_deliveries WHERE "
            "status IN ('held_restart','retryable_restart') "
            "OR status LIKE 'replaying_restart:%'"
        ).fetchone()[0])
    finally:
        if db is None:
            connection.close()


def _replay_candidates() -> list[dict]:
    connection = get_db()
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM webhook_deliveries WHERE "
                "status IN ('held_restart','retryable_restart') OR "
                "(status LIKE 'replaying_restart:%' AND status NOT LIKE ?) "
                "ORDER BY created_at,id",
                (f"replaying_restart:{_BOOT_OWNER_TOKEN}:%",),
            ).fetchall()
        ]
    finally:
        connection.close()


def _claim(delivery_id: str, observed_status: str) -> str | None:
    claim = f"replaying_restart:{_BOOT_OWNER_TOKEN}:{uuid.uuid4().hex}"
    with write_transaction(None, scope="restart_webhook") as connection:
        cur = connection.execute(
            "UPDATE webhook_deliveries SET status=?,error=NULL "
            "WHERE delivery_id=? AND status=?",
            (claim, delivery_id, observed_status),
        )
        return claim if cur.rowcount == 1 else None


def _settle(
    delivery_id: str,
    claim: str,
    *,
    status: str,
    error: str | None = None,
) -> bool:
    with write_transaction(None, scope="restart_webhook") as connection:
        cur = connection.execute(
            "UPDATE webhook_deliveries SET status=?,error=?,payload_json="
            "CASE WHEN ?='dispatched' THEN NULL ELSE payload_json END "
            "WHERE delivery_id=? AND status=?",
            (status, (error or "")[:500] or None, status, delivery_id, claim),
        )
        return cur.rowcount == 1


_DEAD_LETTER_SEED = "corsia-webhook-dead-letter"


def _dead_letter(
    row: dict,
    claim: str,
    *,
    reason: str,
) -> bool:
    """Atomically terminalize the webhook and expose one deterministic dead letter."""
    dead_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{_DEAD_LETTER_SEED}:{row['delivery_id']}",
    ))
    with write_transaction(None, scope="restart_webhook") as connection:
        cur = connection.execute(
            "UPDATE webhook_deliveries SET status='dead_letter_restart',error=? "
            "WHERE delivery_id=? AND status=?",
            (reason[:500], row["delivery_id"], claim),
        )
        if cur.rowcount != 1:
            return False
        connection.execute(
            "INSERT OR IGNORE INTO dead_letter_events "
            "(id,internal_event,external_id,reason,payload_json,status,created_at) "
            "VALUES (?,?,?,?,?,'parked',?)",
            (
                dead_id,
                f"webhook.github.{row['event_type']}",
                row["delivery_id"],
                reason[:500],
                row["payload_json"] or "{}",
                row["created_at"],
            ),
        )
        return True


def retry_dead_lettered_webhook(delivery_id: str) -> bool:
    """Explicitly return one terminal poison row to held replay after correction."""
    with write_transaction(None, scope="restart_webhook") as connection:
        cur = connection.execute(
            "UPDATE webhook_deliveries SET status='held_restart',error=NULL "
            "WHERE delivery_id=? AND status='dead_letter_restart'",
            (delivery_id,),
        )
        if cur.rowcount != 1:
            return False
        connection.execute(
            "UPDATE dead_letter_events SET status='retrying' WHERE external_id=? "
            "AND status='parked'",
            (delivery_id,),
        )
        return True


async def replay_held_webhooks() -> dict[str, list[str]]:
    """Schedule every recoverable row once; poison is visible and never wedges boot."""
    replayed: list[str] = []
    dead_lettered: list[str] = []
    retryable: list[str] = []

    for row in _replay_candidates():
        delivery_id = str(row["delivery_id"])
        claim = _claim(delivery_id, str(row["status"]))
        if claim is None:
            continue
        try:
            payload = json.loads(row["payload_json"] or "{}")
            if not isinstance(payload, dict):
                raise WebhookReplayPoison("stored webhook payload is not an object")
            await dispatch_github_webhook_for_replay(
                str(row["event_type"]),
                payload,
                delivery_id=delivery_id,
            )
        except (WebhookReplayPoison, json.JSONDecodeError) as exc:
            if _dead_letter(row, claim, reason=str(exc)):
                dead_lettered.append(delivery_id)
            continue
        except Exception as exc:  # transient scheduler/runtime failure is operator-visible
            if _settle(
                delivery_id,
                claim,
                status="retryable_restart",
                error=str(exc),
            ):
                retryable.append(delivery_id)
            continue
        if _settle(delivery_id, claim, status="dispatched"):
            replayed.append(delivery_id)
    return {
        "replayed": replayed,
        "dead_lettered": dead_lettered,
        "retryable": retryable,
    }
