"""The presence status is written only where the gate marks a head reviewed."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, patch

import pytest
from reviewer_gate_fixtures import (  # noqa: F401
    _DEFAULT_PR_INFO,
    _call_gate,
    _disable_analytics,
    _reload_gate,
    _stub_presence,
)


def _verdict(verdict: str) -> dict:
    blocks = 1 if verdict == "code_fail" else 0
    return {
        "verdict": verdict,
        "required_action": "none",
        "summary": f"{blocks} block, 0 flag, 0 nit -- reviewed by codex",
        "block_count": blocks,
        "flag_count": 0,
        "nit_count": 0,
        "finding_count": blocks,
        "model_label": "codex",
    }


def _run(gate_mod, verdict, delivered):
    from tests.event_routing_helpers import mk_session, register_pr

    mk_session("owner-thread")
    register_pr("owner/repo", 42, "owner-thread")
    with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
         patch.object(gate_mod, "emit_product_event") as emit_event, \
         patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
         patch("reviewer.on_pr_review.on_pr_review", return_value=verdict) as review, \
         patch("events.delivery.deliver_to_thread", delivered), \
         patch("reviewer.reviewer_status.set_error") as set_error, \
         patch.object(
             gate_mod.github_reviewer_notifications, "notify_reviewer_terminal_error",
         ) as terminal_error:
        _call_gate(gate_mod)
        _call_gate(gate_mod)
    return review, set_error, terminal_error, emit_event


@pytest.mark.parametrize("verdict", ["clean", "code_fail"])
def test_delivered_verdict_posts_presence_and_marks_reviewed(_reload_gate, _stub_presence, verdict):
    review, set_error, terminal_error, _ = _run(_reload_gate, _verdict(verdict), AsyncMock(return_value=True))

    _stub_presence.assert_called_once_with("owner/repo", "abc123def")
    assert _reload_gate.reviewer_gate_state.get_reviewed_sha("owner/repo#42") == "abc123def"
    review.assert_called_once()
    set_error.assert_not_called()
    terminal_error.assert_not_called()


def test_failed_verdict_wake_posts_no_presence(_reload_gate, _stub_presence):
    _run(_reload_gate, _verdict("code_fail"), AsyncMock(return_value=False))

    _stub_presence.assert_not_called()
    assert _reload_gate.reviewer_gate_state.get_reviewed_sha("owner/repo#42") is None


def test_failed_presence_keeps_verdict_and_wake_and_stays_retryable(_reload_gate, _stub_presence):
    _stub_presence.return_value = False
    delivered = AsyncMock(return_value=True)

    review, set_error, terminal_error, emit_event = _run(_reload_gate, _verdict("code_fail"), delivered)

    assert delivered.await_args_list[0].args[3]["verdict"] == "code_fail"
    set_error.assert_not_called()
    terminal_error.assert_called_with("owner/repo", 42, "abc123def", "presence status not published", ANY)
    assert any(
        call.args[:2] == ("reviewer_gate", "presence_unpublished") for call in emit_event.call_args_list
    )
    assert _reload_gate.reviewer_gate_state.get_reviewed_sha("owner/repo#42") is None
    assert review.call_count == 2


def test_override_skip_posts_no_presence(_reload_gate, _stub_presence):
    skipped = {"verdict": "skipped", "summary": "review skipped by override", "model_label": "override"}
    _run(_reload_gate, skipped, AsyncMock(return_value=True))

    _stub_presence.assert_not_called()
    assert _reload_gate.reviewer_gate_state.get_reviewed_sha("owner/repo#42") == "abc123def"
