"""Reviewer receipt and process-local guard for an eligible exact-head review."""

from __future__ import annotations

import threading

_IN_FLIGHT: set[tuple[str, str]] = set()
_IN_FLIGHT_LOCK = threading.Lock()


def _ensure_pending_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reviewer_gate_pending ("
        "pr_key TEXT NOT NULL, head_sha TEXT NOT NULL, "
        "internal_event TEXT NOT NULL, PRIMARY KEY (pr_key, head_sha))"
    )


def get_reviewed_sha(pr_key: str) -> str | None:
    """Return the last reviewed SHA for this PR key, or None."""
    from infra.database import get_db

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT reviewed_sha FROM reviewer_gate_state WHERE pr_key = ?",
            (pr_key,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def mark_reviewed(pr_key: str, head_sha: str) -> None:
    """Persist the SHA after publishing its reviewer verdict."""
    from infra.database import write_transaction

    with write_transaction(None, scope="mark_reviewed") as conn:
        _ensure_pending_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO reviewer_gate_state (pr_key, reviewed_sha) "
            "VALUES (?, ?)",
            (pr_key, head_sha),
        )
        conn.execute(
            "DELETE FROM reviewer_gate_pending WHERE pr_key = ? AND head_sha = ?",
            (pr_key, head_sha),
        )


def record_pending(pr_key: str, head_sha: str, internal_event: str) -> None:
    """Persist a reviewer run before invoking the blocking evaluator."""
    from infra.database import write_transaction

    with write_transaction(None, scope="record_pending_review") as conn:
        _ensure_pending_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO reviewer_gate_pending "
            "(pr_key, head_sha, internal_event) VALUES (?, ?, ?)",
            (pr_key, head_sha, internal_event),
        )


def get_pending() -> list[tuple[str, str, str]]:
    """Return reviewer runs left by a current or prior process."""
    from infra.database import get_db

    conn = get_db()
    try:
        _ensure_pending_table(conn)
        return list(conn.execute(
            "SELECT pr_key, head_sha, internal_event FROM reviewer_gate_pending"
        ).fetchall())
    finally:
        conn.close()


def clear_pending(pr_key: str, head_sha: str) -> None:
    """Remove a pending run that is no longer eligible for this exact head."""
    from infra.database import write_transaction

    with write_transaction(None, scope="clear_pending_review") as conn:
        _ensure_pending_table(conn)
        conn.execute(
            "DELETE FROM reviewer_gate_pending WHERE pr_key = ? AND head_sha = ?",
            (pr_key, head_sha),
        )


def get_approved_sha(pr_key: str) -> str | None:
    """Return the head a maintainer approved for review, or None."""
    from infra.database import get_db

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT approved_sha FROM reviewer_gate_approval WHERE pr_key = ?",
            (pr_key,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def record_approval(pr_key: str, head_sha: str) -> None:
    """Persist the one head a maintainer approved; it replaces any earlier head."""
    from infra.database import write_transaction

    with write_transaction(None, scope="record_review_approval") as conn:
        conn.execute(
            "INSERT OR REPLACE INTO reviewer_gate_approval (pr_key, approved_sha) "
            "VALUES (?, ?)",
            (pr_key, head_sha),
        )


def pr_key(repo: str, pr_number: int) -> str:
    """Build the canonical repository and pull-request key for gate state."""
    return f"{repo}#{pr_number}"


def claim_in_flight(pr_key: str, head_sha: str) -> bool:
    """Claim one eligible reviewer run for this PR head within this process."""
    key = (pr_key, head_sha)
    with _IN_FLIGHT_LOCK:
        if key in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(key)
        return True


def release_in_flight(pr_key: str, head_sha: str) -> None:
    """Release an eligible reviewer run after its terminal attempt."""
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.discard((pr_key, head_sha))
