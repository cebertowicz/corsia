"""Reviewer receipt and process-local guard for an eligible exact-head review."""

from __future__ import annotations

import threading

_IN_FLIGHT: set[tuple[str, str]] = set()
_IN_FLIGHT_LOCK = threading.Lock()


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
        conn.execute(
            "INSERT OR REPLACE INTO reviewer_gate_state (pr_key, reviewed_sha) "
            "VALUES (?, ?)",
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
