"""Restart recovery persists an exact pull-request head until it settles."""

import asyncio
import sqlite3
from contextlib import closing
import sys
import types

import pytest

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


@pytest.mark.asyncio
async def test_startup_recovery_dispatches_saved_review(isolated_db, monkeypatch):
    reviewer_gate_state.record_pending("owner/repo#42", "head-1", "check_suite")
    scheduled = set()
    calls = []
    reviewer_gate = types.ModuleType("events.reviewer_gate")
    reviewer_gate._PENDING_RETRIES = scheduled
    reviewer_gate._evaluate_review_gate = lambda *args: calls.append(args)
    monkeypatch.setitem(sys.modules, "events.reviewer_gate", reviewer_gate)

    from events.reviewer_gate_recovery import recover_pending_reviews

    loop = asyncio.get_running_loop()
    assert recover_pending_reviews(loop) == 1
    await asyncio.gather(*tuple(scheduled))
    assert calls == [("owner/repo", "head-1", 42, "check_suite", loop)]
