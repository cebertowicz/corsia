"""A pull request's own end retires the pane rows that tracked it.

Plan-node settlement spares an open pull request
(``registry.retirement_predicate.PULL_REQUEST_EXEMPT``): a PR outlives the task
that opened it. This module supplies the exit that exemption implies. The PR's
terminal state is recorded on the identity, which ends the exemption, and the
shared retirement writer (``registry.node_retirement``) retires every row in
the same transaction. A database can therefore never hold an ended pull
request that is still claiming pane space unless a user pinned it.

A close is not final: GitHub can reopen the pull request, and that reopen is
the one way back (event rules R27). ``record_pull_request_reopened`` clears
the recorded close and shows the owner its row again in one write, so the
record never says ended for a pull request that is open. A merge stays an end.
"""

from __future__ import annotations

from infra.database import get_db, write_transaction
from plans.public import deliver_notifications as deliver
from registry import exemption_restore, models, node_retirement
from registry.retirement_predicate import PR_TERMINAL_STATES


def record_pull_request_ended(
    integration: str,
    type: str,
    external_id: str,
    *,
    pr_state: str,
    causal_event_id: str | None = None,
    db=None,
) -> dict | None:
    """Record a pull request's terminal state and retire its rows in one write.

    Returns the updated identity row, or ``None`` when no such artifact is
    registered. After commit, every affected thread is nudged and the
    retirement reaches the shared ``artifact_pane.auto_retired`` telemetry.
    """
    if pr_state not in PR_TERMINAL_STATES:
        raise ValueError(
            f"pr_state must be one of {PR_TERMINAL_STATES}, got {pr_state!r}"
        )
    conn = db if db is not None else get_db()
    try:
        floor = node_retirement.capture_event_floor(conn)
        with write_transaction(conn, scope="pull_request_ended") as _db:
            row = models.update_metadata(
                integration,
                type,
                external_id,
                {"merged": pr_state == "merged", "pr_state": pr_state},
                db=_db,
            )
            if row is None:
                return None
            node_retirement.retire_registrations_for_ended_artifact(
                _db,
                artifact_id=str(row["id"]),
                trigger=f"pull_request_{pr_state}",
                causal_event_id=causal_event_id,
            )
        deliver(node_retirement.after_commit(floor, db=conn))
        return row
    finally:
        if db is None:
            conn.close()


def record_pull_request_reopened(
    integration: str, type: str, external_id: str, *, db=None,
) -> dict | None:
    """Clear a recorded close and show the owner its row again in one write.

    Returns the updated identity row, or ``None`` when there is no close to
    undo: the artifact is not registered, was never recorded closed, or was
    merged, which GitHub never reopens and which stays an end here.
    """
    conn = db if db is not None else get_db()
    try:
        with write_transaction(conn, scope="pull_request_reopened") as _db:
            closed = _db.execute(
                "SELECT 1 FROM artifact_identities WHERE integration=? AND type=? "
                "AND external_id=? AND json_extract(metadata, '$.pr_state')='closed'",
                (integration, type, external_id),
            ).fetchone()
            if closed is None:
                return None
            row = models.update_metadata(
                integration, type, external_id, {"merged": None, "pr_state": None}, db=_db,
            )
            # Open again, so exempt from retirement again: the end's retirement lifts.
            exemption_restore.restore_exempt_owner_row(
                _db, artifact_id=str(row["id"]), trigger="pull_request_reopened",
            )
            return row
    finally:
        if db is None:
            conn.close()
