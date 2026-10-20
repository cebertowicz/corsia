"""Webhook delivery persistence, atomic dedup, and restart status CAS.

Dedup is by delivery id (a redelivery) and by GitHub object (the same comment
from the webhook and from the notifications poller), so one comment is
admitted once whichever source records it first.

A ``check_suite`` row also records its check suite id, so the log answers
whether a given workflow run's CI event reached this instance
(``check_suites_received_at``).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from infra.database import get_db, write_transaction

log = logging.getLogger(__name__)

#: Event types whose ``created`` delivery names one GitHub comment object.
_COMMENT_OBJECT_EVENTS = frozenset({"issue_comment", "pull_request_review_comment"})
#: CORSIA-synthesized receipts are not external webhook health signals.
POLL_DELIVERY_PREFIX = "gh-poll:"
NOTIFICATIONS_DELIVERY_PREFIX = "gh-notifications:"
SYNTHETIC_DELIVERY_PREFIXES = (POLL_DELIVERY_PREFIX, NOTIFICATIONS_DELIVERY_PREFIX)


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    row_id: str
    delivery_id: str
    status: str
    created: bool


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def object_key_for(event_type: str, payload: dict[str, Any]) -> str | None:
    """Return the cross-source dedupe key for a new comment, else None."""
    if event_type not in _COMMENT_OBJECT_EVENTS or payload.get("action") != "created":
        return None
    comment = payload.get("comment")
    comment_id = comment.get("id") if isinstance(comment, dict) else None
    if comment_id is None or isinstance(comment_id, bool):
        return None
    return f"{event_type}:{comment_id}"


def check_suite_id_for(event_type: str, payload: dict[str, Any]) -> int | None:
    """The check suite a ``check_suite`` delivery reports on, else None."""
    if event_type != "check_suite":
        return None
    suite = payload.get("check_suite")
    suite_id = suite.get("id") if isinstance(suite, dict) else None
    return suite_id if isinstance(suite_id, int) and not isinstance(suite_id, bool) else None


def check_suites_received_at(
    suites: list[tuple[int, str]], *, db=None,
) -> dict[int, str]:
    """For each ``(check suite id, since)``, when the log first recorded a
    completed delivery for that suite at or after ``since``; absent if none.

    ``since`` is the run's latest attempt start, so a rerun's completion is
    not answered by the attempt before it. Any recorded delivery counts,
    whatever its routing status: it reached this instance. One query.
    """
    if not suites:
        return {}
    connection = db if db is not None else get_db()
    match = " OR ".join(["(check_suite_id=? AND created_at>=?)"] * len(suites))
    rows = connection.execute(
        "SELECT check_suite_id, MIN(created_at) FROM webhook_deliveries "
        f"WHERE event_type='check_suite' AND action='completed' AND ({match}) "
        "GROUP BY check_suite_id",
        [value for suite in suites for value in suite],
    ).fetchall()
    return {int(row[0]): str(row[1]) for row in rows}


#: The last pre-column check-suite row, read only up to the first id-bearing one.
LAST_WITHOUT_ID_SQL = (
    "SELECT MAX(created_at) FROM webhook_deliveries WHERE event_type='check_suite' "
    "AND check_suite_id IS NULL AND created_at <= COALESCE(("
    "SELECT created_at FROM webhook_deliveries WHERE event_type='check_suite' "
    "AND check_suite_id IS NOT NULL ORDER BY created_at LIMIT 1"
    "), '9999-12-31T23:59:59Z')"
)


def last_check_suite_without_id(*, db=None) -> str | None:
    """When the log last recorded a check-suite delivery with no suite id.

    Only rows written before ``check_suite_id`` existed lack one (every
    ``check_suite`` payload carries its id), and they have no payload to
    recover it from. A CI event after this time was either logged with its id
    or not received; None means no such row, so nothing predates the column.
    Reads only up to the first id-bearing row, a frozen legacy set.
    """
    connection = db if db is not None else get_db()
    row = connection.execute(LAST_WITHOUT_ID_SQL).fetchone()
    return row[0] if row else None


def repo_delivered_since(repo: str, since: str, *, db=None) -> bool:
    """Whether any webhook delivery for ``repo`` arrived since the UTC timestamp."""
    return (repo_last_delivered_at(repo, db=db) or "") >= since


def repo_last_delivered_at(repo: str, *, db=None) -> str | None:
    """The latest real GitHub webhook timestamp for a repository, if any."""
    connection = db if db is not None else get_db()
    excluded = " AND ".join("delivery_id NOT LIKE ?" for _ in SYNTHETIC_DELIVERY_PREFIXES)
    row = connection.execute(
        f"SELECT MAX(created_at) FROM webhook_deliveries WHERE repo=? AND {excluded}",
        (repo, *(f"{prefix}%" for prefix in SYNTHETIC_DELIVERY_PREFIXES)),
    ).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def object_recorded(object_key: str, *, db=None) -> bool:
    """Whether any source already recorded this GitHub object.

    Without ``db`` it reads through this thread's pooled connection, so a
    per-comment call opens nothing new and there is nothing to close.
    """
    connection = db if db is not None else get_db()
    row = connection.execute(
        "SELECT 1 FROM webhook_deliveries WHERE object_key=?", (object_key,),
    ).fetchone()
    return row is not None


def record_delivery(
    *,
    event_type: str,
    action: str | None,
    delivery_id: str,
    repo: str | None,
    payload: dict[str, Any],
    status: str = "received",
    db=None,
) -> DeliveryRecord:
    """Atomically dedupe and insert directly into the final initial status."""
    row_id = str(uuid.uuid4())
    object_key = object_key_for(event_type, payload)
    payload_json = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if status == "held_restart"
        else None
    )
    with write_transaction(db, scope="webhook_delivery") as connection:
        cur = connection.execute(
            "INSERT OR IGNORE INTO webhook_deliveries "
            "(id,event_type,action,delivery_id,repo,payload_hash,payload_json,"
            "status,created_at,object_key,check_suite_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                row_id,
                event_type,
                action,
                delivery_id,
                repo,
                _hash_payload(payload),
                payload_json,
                status,
                _now_iso(),
                object_key,
                check_suite_id_for(event_type, payload),
            ),
        )
        if cur.rowcount == 1:
            return DeliveryRecord(row_id, delivery_id, status, True)
        existing = connection.execute(
            "SELECT id,status FROM webhook_deliveries WHERE delivery_id=? "
            "OR (? IS NOT NULL AND object_key=?) "
            "ORDER BY delivery_id=? DESC LIMIT 1",
            (delivery_id, object_key, object_key, delivery_id),
        ).fetchone()
        if existing is None:
            raise RuntimeError("webhook dedup insert lost its canonical row")
        return DeliveryRecord(
            str(existing["id"]),
            delivery_id,
            str(existing["status"]),
            False,
        )


def update_delivery_status(
    delivery_id: str,
    status: str,
    error: str | None = None,
    *,
    expected_status: str | None = None,
    db=None,
) -> bool:
    """CAS a delivery status when an expected claim is supplied."""
    if error:
        error = error[:500]
    with write_transaction(db, scope="webhook_delivery") as connection:
        if expected_status is None:
            cur = connection.execute(
                "UPDATE webhook_deliveries SET status=?,error=? WHERE delivery_id=?",
                (status, error, delivery_id),
            )
        else:
            cur = connection.execute(
                "UPDATE webhook_deliveries SET status=?,error=? "
                "WHERE delivery_id=? AND status=?",
                (status, error, delivery_id, expected_status),
            )
        return cur.rowcount == 1
