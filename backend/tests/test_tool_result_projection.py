from __future__ import annotations

import hashlib
import json

import pytest


def _tool_message(content: str):
    from history.types import PortableMessage

    raw = content.encode("utf-8")
    return PortableMessage(
        message_id="tool-result-1",
        seq=3,
        turn_id="turn-1",
        role="tool",
        content=content,
        created_at="2026-08-04T00:00:00Z",
        assistant_step_id="assistant-1",
        tool_call_id="call-1",
        tool_name="read_file",
        content_sha256=hashlib.sha256(raw).hexdigest(),
        content_byte_count=len(raw),
    )


def _projection(tool):
    from history.types import (
        HistoryBoundary,
        PortableHistoryProjection,
        PortableMessage,
        PortableTurn,
    )

    user = PortableMessage(
        message_id="user-1",
        seq=1,
        turn_id="turn-1",
        role="user",
        content="inspect",
        created_at="2026-08-04T00:00:00Z",
    )
    assistant = PortableMessage(
        message_id="assistant-1",
        seq=2,
        turn_id="turn-1",
        role="assistant",
        content="I inspected it.",
        created_at="2026-08-04T00:00:01Z",
        tool_calls=({
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        },),
    )
    boundary = HistoryBoundary("thread-1", 0, 3, "tool-result-1")
    return PortableHistoryProjection(
        format_version=1,
        thread_id="thread-1",
        history_epoch=0,
        start_exclusive=None,
        sealed_boundary=boundary,
        turns=(PortableTurn(
            turn_id="turn-1",
            status="completed",
            messages=(user, assistant, tool),
            boundary=boundary,
        ),),
        full_raw_replay_available=True,
    )


def test_tool_result_preview_is_exact_when_it_fits() -> None:
    from core.tool_result_projection import render_tool_result_for_model

    tool = _tool_message("exact result")
    assert render_tool_result_for_model(
        "thread-1",
        tool,
        budget_tokens=128,
    ) == "exact result"


def test_bounded_preview_binds_scoped_immutable_reference() -> None:
    from core.token_estimation import _estimate_tokens
    from core.tool_result_projection import render_tool_result_for_model

    content = "HEAD-" + ("middle-" * 2_000) + "-TAIL"
    tool = _tool_message(content)
    preview = render_tool_result_for_model(
        "thread-1",
        tool,
        budget_tokens=512,
    )

    assert _estimate_tokens(preview) <= 512
    assert preview.startswith("HEAD-")
    assert preview.endswith("-TAIL")
    assert "CORSIA_TOOL_RESULT_PREVIEW" in preview
    assert '"thread_id":"thread-1"' in preview
    assert '"message_id":"tool-result-1"' in preview
    assert f'"sha256":"{tool.content_sha256}"' in preview
    assert "corsia://threads/thread-1/messages/tool-result-1/tool-result" in preview
    assert content != preview


def test_destructively_retained_row_never_claims_full_retrieval() -> None:
    from core.thread_lifecycle import TOOL_TRIM_MARKER
    from core.tool_result_projection import (
        ToolResultProjectionError,
        render_tool_result_for_model,
    )

    tool = _tool_message(TOOL_TRIM_MARKER + " unavailable")
    with pytest.raises(ToolResultProjectionError, match="destructively retained"):
        render_tool_result_for_model(
            "thread-1",
            tool,
            budget_tokens=1,
        )


def test_all_destination_adapters_reuse_the_same_tool_preview() -> None:
    from history.adapters import (
        checkpoint_source_block,
        to_native_history,
    )
    from core.tool_result_projection import render_tool_result_for_model
    from subagents.cc_context_reconstruction import cc_snapshot_history
    from subagents.codex_context_reconstruction import codex_inject_items

    tool = _tool_message("HEAD-" + ("middle-" * 2_000) + "-TAIL")
    projection = _projection(tool)
    preview = render_tool_result_for_model(
        projection.thread_id,
        tool,
        budget_tokens=512,
    )

    native = to_native_history(
        projection,
        tool_result_budget_tokens=512,
    )[-1]["content"]
    checkpoint = checkpoint_source_block(
        tool,
        thread_id=projection.thread_id,
        tool_result_budget_tokens=512,
    )["content"]
    cc = cc_snapshot_history(
        projection,
        cc_session_id="cc-session",
        tool_result_budget_tokens=512,
    )[-1]["content"][0]["content"][0]["text"]
    codex = codex_inject_items(
        projection,
        tool_result_budget_tokens=512,
    )[-1]["content"][0]["text"]

    assert preview == native
    assert preview in checkpoint
    assert preview == cc
    assert preview in codex
    full_content = tool.content
    assert all(full_content not in rendered for rendered in (
        native,
        checkpoint,
        cc,
        codex,
    ))


def test_scoped_resolver_rejects_cross_thread_and_digest_mismatch(
    isolated_db,
) -> None:
    from core.tool_result_projection import (
        ToolResultReferenceError,
        resolve_tool_result_reference,
        tool_result_reference,
    )
    from infra.database import get_db

    content = "full canonical bytes"
    raw = content.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/test','t','t')"
    )
    db.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            "tool-result-1",
            "thread-1",
            "tool",
            content,
            json.dumps({
                "tool_call_id": "call-1",
                "name": "read_file",
                "tool_result_sha256": digest,
                "tool_result_byte_count": len(raw),
            }),
            "t",
            1,
        ),
    )
    db.commit()

    reference = tool_result_reference(
        thread_id="thread-1",
        message_id="tool-result-1",
        sha256=digest,
        byte_count=len(raw),
    )
    assert resolve_tool_result_reference(
        reference,
        thread_id="thread-1",
        db=db,
    ) == content

    with pytest.raises(ToolResultReferenceError, match="another thread"):
        resolve_tool_result_reference(
            reference,
            thread_id="thread-2",
            db=db,
        )

    wrong_digest = {**reference, "sha256": "0" * 64}
    with pytest.raises(ToolResultReferenceError, match="digest"):
        resolve_tool_result_reference(
            wrong_digest,
            thread_id="thread-1",
            db=db,
        )
    with pytest.raises(ToolResultReferenceError, match="invalid digest"):
        tool_result_reference(
            thread_id="thread-1",
            message_id="tool-result-1",
            sha256=digest.upper(),
            byte_count=len(raw),
        )

    legacy = "legacy full bytes"
    legacy_raw = legacy.encode("utf-8")
    legacy_digest = hashlib.sha256(legacy_raw).hexdigest()
    db.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            "legacy-tool-result",
            "thread-1",
            "tool",
            legacy,
            json.dumps({"tool_call_id": "legacy-call", "name": "read_file"}),
            "t",
            2,
        ),
    )
    db.commit()
    legacy_reference = tool_result_reference(
        thread_id="thread-1",
        message_id="legacy-tool-result",
        sha256=legacy_digest,
        byte_count=len(legacy_raw),
    )
    assert resolve_tool_result_reference(
        legacy_reference,
        thread_id="thread-1",
        db=db,
    ) == legacy
    db.close()
