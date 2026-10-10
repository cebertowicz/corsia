"""Retention must preserve replay without promising deleted full payloads."""

from dataclasses import replace
from datetime import timedelta
import hashlib
import json

import pytest

from core.thread_lifecycle_trims import RetentionReport, _trim_tool_payloads
from core.tool_result_projection import (
    ToolResultProjectionError,
    ToolResultReferenceError,
    render_tool_result_for_model,
    resolve_tool_result_reference,
    tool_result_reference,
)
from history.messages import _portable_message
from test_thread_lifecycle import CFG, NOW, _add_session, _db, _iso, _seed_covering_checkpoint
from test_tool_result_projection import _projection


@pytest.fixture
def retained_result():
    """Run the real retention writer against a full, stamped tool result."""
    db = _db()
    _add_session(db, "thread-1", last_activity=NOW - timedelta(days=1))
    content = "Original café result\n" * 500
    original = {
        "tool_result_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "tool_result_byte_count": len(content.encode()),
        "turn_id": "turn-1", "assistant_step_id": "assistant-1",
        "tool_call_id": "call-1", "name": "read_file", "is_error": True,
    }
    db.execute(
        "INSERT INTO messages (id,thread_id,seq,role,content,metadata,created_at) "
        "VALUES ('tool-result-1','thread-1',3,'tool',?,?,?)",
        (content, json.dumps(original), _iso(NOW - timedelta(days=31))),
    )
    db.commit()
    _seed_covering_checkpoint(db, "thread-1")
    report = RetentionReport()
    _trim_tool_payloads(db, NOW, CFG, report)
    assert report.tool_trimmed == 1
    row = dict(db.execute("SELECT * FROM messages WHERE id='tool-result-1'").fetchone())
    row["_meta"] = json.loads(row["metadata"])
    try:
        yield db, row, original
    finally:
        db.close()


@pytest.mark.parametrize("destination", ["native", "cc", "codex", "checkpoint", "db_rebuild"])
def test_retention_then_replay_preserves_notice_in_every_destination(retained_result, destination):
    from core.message_history import _tool_history_message
    from history.adapters import checkpoint_source_block, to_native_history
    from subagents.cc_context_reconstruction import cc_snapshot_history
    from subagents.codex_context_reconstruction import codex_inject_items

    _, row, original = retained_result
    tool = _portable_message(row, "turn-1")
    projection = _projection(tool)
    if destination == "native":
        rendered = to_native_history(projection)[-1]["content"]
    elif destination == "cc":
        rendered = cc_snapshot_history(projection, cc_session_id="cc-1")[-1]["content"][0]["content"][0]["text"]
    elif destination == "codex":
        rendered = codex_inject_items(projection)[-1]["content"][0]["text"]
    elif destination == "checkpoint":
        rendered = checkpoint_source_block(tool, thread_id="thread-1")["content"]
    else:
        rendered = _tool_history_message(row, tool_result_budget_tokens=4096)["content"]

    assert row["content"].startswith("[tool output trimmed")
    assert row["content"] in rendered
    assert "CORSIA_TOOL_RESULT_PREVIEW" not in rendered
    assert row["_meta"] == {
        key: value for key, value in original.items()
        if key not in {"tool_result_sha256", "tool_result_byte_count"}
    }


def test_retention_never_restores_full_payload_reference_and_remains_idempotent(retained_result):
    db, row, original = retained_result
    reference = tool_result_reference(
        thread_id="thread-1", message_id="tool-result-1",
        sha256=original["tool_result_sha256"], byte_count=original["tool_result_byte_count"],
    )
    with pytest.raises(ToolResultReferenceError, match="no full payload"):
        resolve_tool_result_reference(reference, thread_id="thread-1", db=db)
    tool = _portable_message(row, "turn-1")
    with pytest.raises(ToolResultProjectionError, match="destructively retained"):
        render_tool_result_for_model("thread-1", tool, budget_tokens=1)
    report = RetentionReport()
    _trim_tool_payloads(db, NOW, CFG, report)
    assert report.tool_trimmed == 0
    current = dict(db.execute("SELECT * FROM messages WHERE id='tool-result-1'").fetchone())
    assert current == {key: value for key, value in row.items() if key != "_meta"}


@pytest.mark.parametrize("changed_field", ["content_sha256", "content_byte_count"])
def test_full_result_integrity_still_rejects_changed_bytes(changed_field):
    from test_tool_result_projection import _tool_message

    tool = _tool_message("unchanged full result")
    altered = replace(tool, **{changed_field: "0" * 64 if changed_field == "content_sha256" else 1})
    with pytest.raises(ToolResultProjectionError, match="mismatch"):
        render_tool_result_for_model("thread-1", altered, budget_tokens=4096)
