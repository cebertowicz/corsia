"""Restart recovery persists an exact pull-request head until it settles."""

import asyncio
import sqlite3
from contextlib import closing
from unittest.mock import patch

from events import reviewer_gate_state
from reviewer_gate_fixtures import _DEFAULT_PR_INFO, _reload_gate  # noqa: F401


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


def test_recovery_re_evaluates_each_pending_head_and_skips_a_malformed_key(_reload_gate):
    from events import reviewer_gate_recovery

    reviewer_gate_state.record_pending("owner/repo#42", "abc123def", "check_suite")
    reviewer_gate_state.record_pending("not-a-pr-key", "head-2", "check_suite")

    async def run_inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def recover():
        loop = asyncio.get_running_loop()
        dispatched = await reviewer_gate_recovery.recover_pending_reviews(loop)
        await asyncio.gather(*reviewer_gate_recovery._RECOVERED)
        return loop, dispatched

    with patch.object(reviewer_gate_recovery, "run_in_db", run_inline), \
         patch.object(reviewer_gate_recovery.reviewer_gate, "on_pull_request_ready_for_review") as evaluate:
        loop, dispatched = asyncio.run(recover())

    assert dispatched == 1
    evaluate.assert_called_once_with("owner/repo", "abc123def", 42, "check_suite", loop)


def test_a_pr_closed_while_down_clears_its_row_without_a_review(_reload_gate):
    gate_mod = _reload_gate
    reviewer_gate_state.record_pending("owner/repo#42", "abc123def", "check_suite")
    merged = {**_DEFAULT_PR_INFO, "state": "closed", "mergeable": "UNKNOWN"}

    with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
         patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=merged) as fetch, \
         patch.object(gate_mod, "_run_reviewer") as review, \
         patch.object(gate_mod, "_report_mergeability_unresolved") as unresolved, \
         patch.object(gate_mod, "emit_product_event"):
        gate_mod.on_pull_request_ready_for_review("owner/repo", "abc123def", 42, "check_suite")

    fetch.assert_called_once()
    review.assert_not_called()
    unresolved.assert_not_called()
    assert reviewer_gate_state.get_pending() == []


def test_a_head_replaced_while_down_clears_its_row_without_a_review(_reload_gate):
    gate_mod = _reload_gate
    reviewer_gate_state.record_pending("owner/repo#42", "abc123def", "check_suite")
    moved = {**_DEFAULT_PR_INFO, "headRefOid": "fed321cba"}

    with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
         patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=moved), \
         patch.object(gate_mod, "_run_reviewer") as review, \
         patch.object(gate_mod, "emit_product_event"):
        gate_mod.on_pull_request_ready_for_review("owner/repo", "abc123def", 42, "check_suite")

    review.assert_not_called()
    assert reviewer_gate_state.get_pending() == []
