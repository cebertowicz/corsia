"""Read-time usage-limit visibility, including live row projection."""

import pytest

from infra.database import get_db
from infra.focus_rows import is_focus_visible
from infra.send_broadcast import InputBroadcastRow, project_input_message


def test_pooled_connection_registers_the_shared_classifier():
    db = get_db()
    assert db.execute("SELECT is_provider_usage_limit(?)", ("You've hit your 5-hour limit",)).fetchone()[0] == 1
    assert db.execute("SELECT is_provider_usage_limit(?)", ("rate limited",)).fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["sign_in_expired", "context_overflow", "rate_limited", "process_exited"])
def test_other_errors_remain_visible(kind):
    row = {"role": "system", "content": "ordinary failure", "metadata": {"type": "cc_error", "cause_kind": kind}}
    assert is_focus_visible(row)


@pytest.mark.parametrize("content", ["You've hit your usage limit", "Process exited while checking billing quota"])
@pytest.mark.parametrize("kind", ["llm_error", "cc_error", "result"])
@pytest.mark.parametrize("cause, hidden", [(None, True), ("", True), ("unknown", True),
                                           ("usage_limit", True), ("process_exited", False),
                                           ("sign_in_expired", False), ("rate_limited", False)])
def test_live_projection_respects_cause_with_limit_wording(kind, cause, hidden, content):
    metadata = {"type": kind, "is_error": True}
    if cause is not None:
        metadata["cause_kind"] = cause
    row = InputBroadcastRow("id", content, metadata, "2026-01-01", "delivered", None, 1, 1, role="system")
    projected = project_input_message(row)
    assert (projected["metadata"].get("chat_drawn") is False) is hidden


def test_live_projection_hides_codex_result_without_changing_stored_metadata():
    metadata = {"codex": True, "type": "result", "is_error": True}
    row = InputBroadcastRow("id", "You've hit your usage limit", metadata, "2026-01-01", "delivered", None, 1, 1, role="system")
    projected = project_input_message(row)
    assert projected["metadata"]["chat_drawn"] is False
    assert metadata == {"codex": True, "type": "result", "is_error": True}
