"""Restart recovery persists an exact pull-request head until it settles."""

import sqlite3
from contextlib import closing

from events import reviewer_gate_state


def test_pending_review_survives_restart_and_is_cleared_by_verdict(isolated_db):
    reviewer_gate_state.record_pending("owner/repo#42", "head-1", "check_suite")

    with closing(sqlite3.connect(str(isolated_db))) as db:
        assert db.execute(
            "SELECT pr_key, head_sha, internal_event FROM reviewer_gate_pending"
        ).fetchone() == ("owner/repo#42", "head-1", "check_suite")

    assert reviewer_gate_state.get_pending() == [
        ("owner/repo#42", "head-1", "check_suite")
    ]
    reviewer_gate_state.mark_reviewed("owner/repo#42", "head-1")
    assert reviewer_gate_state.get_pending() == []
