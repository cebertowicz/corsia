"""Fixed, caller-owned read-only audit metrics over the state database."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from host_access.failure_reasons import safe_rejection
from infra.checkpoint_queries import current_installed_through_seq
from infra.config import DB_PATH
from infra.product_events import emit_product_event
from infra.queries import thread_available_sql

AUDIT_METRICS = ("checkpoint_health", "lease_reviews", "stored_message_pressure")
MAX_WINDOW = timedelta(days=7)
MAX_ROWS = 50
TOP_PRESSURE_ROWS = 10
MAX_BYTES = 16 * 1024
TIMEOUT_SECONDS = 1.5


class AuditReadError(Exception):
    """A safe, categorized audit refusal."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


def _utc(value: str | None) -> datetime:
    if not isinstance(value, str):
        raise AuditReadError("invalid_window", "Supply UTC since and until timestamps.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditReadError("invalid_window", "Use ISO 8601 UTC timestamps.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise AuditReadError("invalid_window", "Use ISO 8601 UTC timestamps.")
    return parsed.astimezone(timezone.utc)


def _window(metric: str, since: str | None, until: str | None) -> tuple[str | None, str | None]:
    if metric == "stored_message_pressure" and since is None and until is None:
        return None, None
    start, end = _utc(since), _utc(until)
    if start >= end or end - start > MAX_WINDOW:
        raise AuditReadError("invalid_window", "Use a positive window of no more than seven days.")
    return start.isoformat(), end.isoformat()


def _timeout_error(metric: str, since: str | None) -> AuditReadError:
    if metric == "stored_message_pressure":
        detail = (
            "Audit read timed out; supply since and until to rank recently updated threads."
            if since is None else "Audit read timed out; narrow the activity window."
        )
    else:
        detail = "Audit read timed out; narrow the window."
    return AuditReadError("timeout", detail)


def _query(db: sqlite3.Connection, metric: str, user_id: str, project_id: str,
           since: str | None, until: str | None, deadline: float) -> list[dict]:
    scope = (user_id, project_id)
    if metric == "checkpoint_health":
        cursor = db.execute(
            "WITH scoped_threads AS ("
            "  SELECT t.id FROM threads t WHERE t.user_id=? AND t.project_id=? "
            f"  AND {thread_available_sql('t')}"
            "), latest_installed AS ("
            "  SELECT later.thread_id, max(julianday(later.started_at)) AS started_at "
            "  FROM checkpoints later JOIN scoped_threads t ON t.id=later.thread_id "
            "  WHERE later.status='installed' GROUP BY later.thread_id"
            ") "
            "SELECT c.trigger, c.status, count(*) AS n, "
            "coalesce(sum(c.status='failed' AND "
            "julianday(c.started_at)<latest_installed.started_at),0) AS recovered "
            "FROM checkpoints c JOIN scoped_threads t ON t.id=c.thread_id "
            "LEFT JOIN latest_installed ON latest_installed.thread_id=c.thread_id "
            "WHERE julianday(c.started_at)>=julianday(?) "
            "AND julianday(c.started_at)<julianday(?) "
            "GROUP BY c.trigger,c.status ORDER BY c.trigger,c.status LIMIT ?",
            (*scope, since, until, MAX_ROWS + 1),
        )
    elif metric == "lease_reviews":
        cursor = db.execute(
            "SELECT l.state, CASE WHEN l.state='rejected' THEN "
            "safe_rejection_category(CASE WHEN json_valid(l.application_review_result) "
            "THEN json_extract(l.application_review_result,'$.reason_category') "
            "ELSE NULL END) ELSE NULL END AS reason_category, count(*) AS n "
            "FROM host_access_leases l JOIN threads t ON t.id=l.root_thread_id "
            "WHERE t.user_id=? AND t.project_id=? AND l.user_id=? "
            f"AND {thread_available_sql('t')} "
            "AND julianday(l.created_at)>=julianday(?) "
            "AND julianday(l.created_at)<julianday(?) "
            "GROUP BY l.state,reason_category ORDER BY l.state,reason_category LIMIT ?",
            (*scope, user_id, since, until, MAX_ROWS + 1),
        )
    else:
        # Stored-message pressure is a proxy. Only the checkpoint owner
        # chooses the installed cut; a missing cut has its own labeled state.
        activity_clause = (
            " AND julianday(t.updated_at)>=julianday(?) "
            "AND julianday(t.updated_at)<julianday(?)"
            if since is not None else ""
        )
        activity_params = (since, until) if since is not None else ()
        threads = db.execute(
            "SELECT t.id FROM threads t WHERE t.user_id=? AND t.project_id=? "
            f"AND {thread_available_sql('t')}{activity_clause}",
            (*scope, *activity_params),
        )
        rows: list[dict] = []
        for thread in threads:
            if time.monotonic() >= deadline:
                raise _timeout_error(metric, since)
            through_seq = current_installed_through_seq(thread["id"], db=db)
            counts = db.execute(
                "SELECT count(*) AS stored_messages, "
                "coalesce(sum(length(content)),0)/4 AS approx_stored_tokens "
                "FROM messages WHERE thread_id=? AND seq>?",
                (thread["id"], through_seq if through_seq is not None else 0),
            ).fetchone()
            if counts["stored_messages"]:
                rows.append({
                    "thread_id": thread["id"],
                    "checkpoint_state": "installed" if through_seq is not None else "no_checkpoint",
                    **dict(counts),
                })
                rows.sort(key=lambda row: (-row["approx_stored_tokens"], row["thread_id"]))
                del rows[TOP_PRESSURE_ROWS:]
        return rows
    rows = [dict(row) for row in cursor.fetchall()]
    if metric == "checkpoint_health":
        for row in rows:
            row["unrecovered"] = row["n"] - (row["recovered"] or 0) if row["status"] == "failed" else 0
    if len(rows) > MAX_ROWS:
        raise AuditReadError("result_limit", "Audit result exceeds the fixed row limit; narrow the window.")
    return rows


def _read_audit_metric(
    actor_thread_id: str,
    user_id: str | None,
    metric: str,
    *,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """Read one fixed metric after binding the actor to its owned project."""
    if metric not in AUDIT_METRICS:
        raise AuditReadError("invalid_metric", "Choose checkpoint_health, lease_reviews, or stored_message_pressure.")
    start, end = _window(metric, since, until)
    if not actor_thread_id:
        raise AuditReadError("scope_denied", "An authenticated caller thread is required.")
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        with closing(sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=250")
            db.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 1000)
            db.create_function(
                "safe_rejection_category", 1,
                lambda value: safe_rejection(value)[0], deterministic=True,
            )
            # Bind identity and all metric rows to one read snapshot.
            db.execute("SAVEPOINT scoped_audit_read")
            actor = db.execute(
                "SELECT user_id,project_id FROM threads t WHERE t.id=? "
                f"AND {thread_available_sql('t')}", (actor_thread_id,)
            ).fetchone()
            if (
                actor is None
                or not actor["user_id"]
                or not actor["project_id"]
                or (user_id is not None and actor["user_id"] != user_id)
            ):
                raise AuditReadError(
                    "scope_denied", "Audit reads require a caller-owned thread in a project."
                )
            rows = _query(db, metric, actor["user_id"], actor["project_id"], start, end, deadline)
            if time.monotonic() >= deadline:
                raise _timeout_error(metric, since)
            db.execute("RELEASE scoped_audit_read")
    except sqlite3.Error as exc:
        if "interrupted" in str(exc).lower() or time.monotonic() >= deadline:
            raise _timeout_error(metric, start) from exc
        raise AuditReadError("unavailable", "Audit store is unavailable; use a reviewed lease if authorized.") from exc
    result: dict[str, object] = {
        "metric": metric,
        "scope": "caller_user_and_project",
        "window": [start, end] if start is not None else None,
        "rows": rows,
    }
    if metric == "stored_message_pressure":
        result["rank_limit"] = TOP_PRESSURE_ROWS
    if len(json.dumps(result, separators=(",", ":")).encode("utf-8")) > MAX_BYTES:
        raise AuditReadError("result_limit", "Audit result exceeds the byte limit; narrow the window.")
    return result


def read_audit_metric(
    actor_thread_id: str,
    user_id: str | None,
    metric: str,
    *,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """Read a fixed metric and count success or denial without private contents."""
    metric_name = metric if metric in AUDIT_METRICS else "invalid"
    try:
        result = _read_audit_metric(
            actor_thread_id, user_id, metric, since=since, until=until
        )
    except AuditReadError as exc:
        emit_product_event(
            "audit_read", "denied",
            properties={"metric": metric_name, "category": exc.category},
        )
        raise
    emit_product_event(
        "audit_read", "succeeded", properties={"metric": metric_name}
    )
    return result
