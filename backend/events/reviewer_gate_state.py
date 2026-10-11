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


def claim_workflow_rerun(pr_key: str, run_id: int, attempt: int) -> bool:
    """Durably claim one Actions re-run attempt in the existing gate state.

    The primary PR key remains exclusively for a reviewer verdict receipt. Its
    namespaced sibling key is an action receipt, so a restart cannot submit the
    same Actions run attempt twice and no second state registry is introduced.
    """
    from infra.database import write_transaction

    action_key = f"{pr_key}:rerun:{run_id}:{attempt}"
    with write_transaction(None, scope="claim_workflow_rerun") as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO reviewer_gate_state (pr_key, reviewed_sha) VALUES (?, ?)",
            (action_key, "rerun"),
        )
        return cursor.rowcount == 1


def release_workflow_rerun(pr_key: str, run_id: int, attempt: int) -> None:
    """Release a failed re-run request so a later sweep may retry it."""
    from infra.database import write_transaction

    action_key = f"{pr_key}:rerun:{run_id}:{attempt}"
    with write_transaction(None, scope="release_workflow_rerun") as conn:
        conn.execute("DELETE FROM reviewer_gate_state WHERE pr_key = ?", (action_key,))
