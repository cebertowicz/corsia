"""An idle fold on a harness thread proves its boundary by the harness's own id.

A transcript or rollout the harness wrote itself never names a row by CORSIA's
row id, so since #1781 every idle fold after a session's first Turn was refused
(#2102). The settled Turn's final assistant row now records the harness's id and
the boundary proof accepts it. These folds run the real idle path end to end:
projection, warm proof over a file on disk, marker consumption, and admission.
Only model resolution, the harness session lookup and the provider launch are
replaced.
"""

from __future__ import annotations

import json
import sqlite3
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from core.fork_codex_root import RegisteredRollout

PROVIDER_THREAD_ID = "01a0c52d-18a0-7271-975c-844daef602a5"
# Not a word, so the tail measures well above the idle minimum.
_USER_TEXT = " ".join(f"w{index}" for index in range(40_000))


def _line(record: dict) -> bytes:
    return json.dumps(record, separators=(",", ":")).encode() + b"\n"


def _target(harness: str, model: str):
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id=model,
        harness=harness,
        provider=harness,
        backend="harness",
        transport_model="wire",
        expected_reported_model=None,
        context_window=1_000_000,
        available=True,
        availability_reason=None,
        display_name=harness,
        supports_reasoning=True,
        preserve_thinking_default=True,
    )


def _seed_settled_turn(
    db_path: Path, thread_id: str, model: str, provider: str, provider_message_id: str,
    *, leading_rows: int = 0,
) -> str:
    """One completed Turn with a pending idle marker; return its final assistant row.

    ``leading_rows`` earlier rows share the final row's harness id, as when Claude
    streams one message's thinking and text blocks as separate events.
    """
    from infra.queries import insert_message
    from subagents.child_thread import persist_assistant_step

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?,'steve',?,'t0','t0')",
            (thread_id, model),
        )
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
            "started_at,ended_at,boot_epoch,idle_checkpoint_pending) "
            "VALUES ('turn-1',?,0,?,'completed','t0','t1','boot',1)",
            (thread_id, model),
        )
        insert_message(thread_id, "user", _USER_TEXT, {"turn_id": "turn-1"}, db=db)
        for _ in range(leading_rows):
            persist_assistant_step(
                thread_id, turn_id="turn-1", content="", reasoning="Looking.", db=db,
                provider=provider, provider_message_id=provider_message_id,
            )
        row_id = persist_assistant_step(
            thread_id, turn_id="turn-1", content="Fixed.", db=db,
            provider=provider, provider_message_id=provider_message_id,
        )
        db.commit()
        return row_id
    finally:
        db.close()


def _patch_idle_environment(monkeypatch, target, binary: str) -> tuple[list, list]:
    """Replace model resolution, the provider launch and event export only."""
    from core import checkpoint_runtime

    launched: list = []
    events: list = []
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target", lambda _model, db=None: target,
    )
    monkeypatch.setattr(
        "core.fork_render.fork_binary", lambda _target, _db: binary,
    )
    monkeypatch.setattr(
        checkpoint_runtime, "_launch_checkpoint",
        lambda request, provider_request: launched.append((request, provider_request)),
    )
    monkeypatch.setattr(
        "infra.product_events.emit_product_event",
        lambda _category, action, **kwargs: events.append((action, kwargs)),
    )
    return launched, events


async def _fire(thread_id: str):
    from infra.idle_checkpoints import IdleCheckpointDue
    from thread import idle_checkpoint

    await idle_checkpoint.fire_idle_checkpoint(
        IdleCheckpointDue("turn-1", 0, thread_id, "2026-01-01T00:00:00"),
    )


def _marker_pending(db_path: Path) -> int:
    db = sqlite3.connect(str(db_path))
    try:
        return db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id='turn-1'",
        ).fetchone()[0]
    finally:
        db.close()


def _assert_admitted_warm(launched, events, db_path, thread_id, row_id, harness) -> None:
    ((request, provider_request),) = launched
    assert request.trigger == "idle"
    assert request.mode == "warm"
    assert request.through_message_id == row_id
    assert provider_request.warm_source is not None
    assert provider_request.cold_source is None
    assert events == [(
        "started",
        {
            "thread_id": thread_id,
            "properties": {
                "reason": "fired",
                "through_seq": request.through_seq,
                "writer_harness": harness,
            },
        },
    )]
    assert _marker_pending(db_path) == 0


@pytest.mark.asyncio
async def test_claude_code_idle_fold_is_admitted_warm_by_claude_message_id(
    isolated_db, tmp_path, monkeypatch,
) -> None:
    from subagents.cc_invocation import ClaudeInvocation

    thread_id = "cc-idle"
    row_id = _seed_settled_turn(
        isolated_db, thread_id, "claude-code/test", "cc", "msg_01FinalAssistant",
    )
    transcript = tmp_path / "live-session.jsonl"
    # Claude wrote this itself: it names the row only by Claude's message.id.
    transcript.write_bytes(b"".join((
        _line({"type": "user", "uuid": "cc-user",
               "message": {"role": "user", "content": _USER_TEXT}}),
        _line({"type": "assistant", "uuid": "cc-final",
               "message": {"id": "msg_01FinalAssistant", "role": "assistant",
                           "content": [{"type": "text", "text": "Fixed."}]}}),
        _line({"type": "system", "subtype": "turn_duration", "durationMs": 1200}),
    )))
    (tmp_path / "seed.txt").write_text("seed")
    (tmp_path / "tools.json").write_text("{}")
    invocation = ClaudeInvocation(
        binary="claude",
        permission_mode="bypassPermissions",
        resume_session_id="live-session",
        seed_file=str(tmp_path / "seed.txt"),
        mcp_config=str(tmp_path / "tools.json"),
        working_dir=str(tmp_path),
        model="sonnet",
        effort="high",
    )
    monkeypatch.setattr(
        "core.fork_cc._registered_identity",
        lambda tid: ("live-session", invocation, None, str(transcript))
        if tid == thread_id else None,
    )
    launched, events = _patch_idle_environment(
        monkeypatch, _target("claude-code", "claude-code/test"), "claude",
    )

    await _fire(thread_id)

    _assert_admitted_warm(launched, events, isolated_db, thread_id, row_id, "claude-code")
    assert launched[0][1].warm_source.transcript_bytes == transcript.stat().st_size


@pytest.mark.asyncio
async def test_claude_message_split_across_rows_folds_after_its_whole_group(
    isolated_db, tmp_path, monkeypatch,
) -> None:
    from subagents.cc_invocation import ClaudeInvocation

    thread_id = "cc-split"
    row_id = _seed_settled_turn(
        isolated_db, thread_id, "claude-code/test", "cc", "msg_01Split", leading_rows=1,
    )
    transcript = tmp_path / "live-session.jsonl"
    transcript.write_bytes(b"".join((
        _line({"type": "user", "uuid": "cc-user",
               "message": {"role": "user", "content": _USER_TEXT}}),
        _line({"type": "assistant", "uuid": "cc-thinking",
               "message": {"id": "msg_01Split", "role": "assistant",
                           "content": [{"type": "thinking", "thinking": "Looking."}]}}),
        _line({"type": "assistant", "uuid": "cc-text",
               "message": {"id": "msg_01Split", "role": "assistant",
                           "content": [{"type": "text", "text": "Fixed."}]}}),
    )))
    (tmp_path / "seed.txt").write_text("seed")
    (tmp_path / "tools.json").write_text("{}")
    invocation = ClaudeInvocation(
        binary="claude", permission_mode="bypassPermissions",
        resume_session_id="live-session", seed_file=str(tmp_path / "seed.txt"),
        mcp_config=str(tmp_path / "tools.json"), working_dir=str(tmp_path),
        model="sonnet", effort="high",
    )
    monkeypatch.setattr(
        "core.fork_cc._registered_identity",
        lambda tid: ("live-session", invocation, None, str(transcript))
        if tid == thread_id else None,
    )
    launched, events = _patch_idle_environment(
        monkeypatch, _target("claude-code", "claude-code/test"), "claude",
    )

    await _fire(thread_id)

    _assert_admitted_warm(launched, events, isolated_db, thread_id, row_id, "claude-code")
    assert launched[0][1].warm_source.transcript_bytes == transcript.stat().st_size


def test_only_the_last_row_sharing_a_harness_id_is_a_warm_boundary(isolated_db) -> None:
    """The proof cuts after the id's whole group, so an earlier row would over-cut."""
    from core.fork_render import _durable_warm_boundary

    thread_id = "cc-group"
    last_id = _seed_settled_turn(
        isolated_db, thread_id, "claude-code/test", "cc", "msg_01Group", leading_rows=1,
    )
    db = sqlite3.connect(str(isolated_db))
    try:
        rows = db.execute(
            "SELECT id, seq FROM messages WHERE thread_id=? AND role='assistant' "
            "ORDER BY seq", (thread_id,),
        ).fetchall()
    finally:
        db.close()
    (earlier_id, earlier_seq), (final_id, final_seq) = rows
    assert final_id == last_id

    for harness in ("claude-code", "codex"):
        assert _durable_warm_boundary(
            thread_id, SimpleNamespace(seq=earlier_seq, message_id=earlier_id), harness,
        ) is None
        warm = _durable_warm_boundary(
            thread_id, SimpleNamespace(seq=final_seq, message_id=final_id), harness,
        )
        assert warm is not None and warm.provider_message_id == "msg_01Group"


def _codex_rollout(sessions: Path, item_id: str) -> Path:
    """A rollout in codex-cli 0.155.1's shape: the item id is on both records."""
    token_count = {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {
            "input_tokens": 12, "cached_input_tokens": 3, "output_tokens": 4,
        },
    }}}
    rollout = sessions / f"rollout-2026-09-21T11-14-22-{PROVIDER_THREAD_ID}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_bytes(b"".join((
        _line({"type": "session_meta",
               "payload": {"id": PROVIDER_THREAD_ID, "cwd": str(sessions.parent)}}),
        _line({"type": "response_item", "payload": {
            "type": "message", "role": "user", "id": "msg_user",
            "content": [{"type": "input_text", "text": "please fix it"}]}}),
        _line({"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "AgentMessage", "id": item_id,
            "content": [{"type": "Text", "text": "Fixed."}]}}}),
        _line({"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "id": item_id,
            "content": [{"type": "output_text", "text": "Fixed."}]}}),
        _line({"type": "token_usage_record", "payload": {}}),
        _line(token_count),
        _line({"type": "event_msg", "payload": {"type": "task_complete"}}),
    )))
    return rollout


@pytest.mark.asyncio
async def test_codex_idle_fold_is_admitted_warm_by_codex_item_id(
    isolated_db, tmp_path, monkeypatch,
) -> None:
    thread_id = "codex-idle"
    item_id = "msg_031585a640abb93a016ab17415255487d0a456247b78a06556"
    row_id = _seed_settled_turn(isolated_db, thread_id, "codex/test", "codex", item_id)
    rollout = _codex_rollout(tmp_path / "sessions" / "2026" / "09" / "21", item_id)
    monkeypatch.setattr(
        "core.fork_codex._registered_rollout",
        lambda tid: RegisteredRollout(PROVIDER_THREAD_ID, rollout, "high", None) if tid == thread_id else None,
    )
    launched, events = _patch_idle_environment(
        monkeypatch, _target("codex", "codex/test"), "codex",
    )

    await _fire(thread_id)

    _assert_admitted_warm(launched, events, isolated_db, thread_id, row_id, "codex")
    task_complete = len(_line({"type": "event_msg", "payload": {"type": "task_complete"}}))
    assert launched[0][1].warm_source.rollout_bytes == rollout.stat().st_size - task_complete


def _codex_boundary(provider_message_id: str | None):
    from history.types import DurableBoundaryMessage

    return DurableBoundaryMessage(
        role="assistant", content="Fixed.", message_id="corsia-row",
        assistant_step_id="corsia-row", provider_message_id=provider_message_id,
    )


def test_codex_boundary_with_an_item_id_is_never_proven_by_the_row_id() -> None:
    """A row that carries Codex's id fails closed when that item is absent."""
    from llm.codex_rollout_boundary import codex_rollout_boundary_prefix_bytes

    row_id_record = _line({"type": "response_item", "payload": {
        "type": "message", "role": "assistant", "id": "corsia-row"}})
    rollout = b"".join((
        row_id_record,
        _line({"type": "token_usage_record", "payload": {}}),
        _line({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {
                "input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1,
            },
        }}}),
        _line({"type": "event_msg", "payload": {"type": "task_complete"}}),
    ))

    assert codex_rollout_boundary_prefix_bytes(
        BytesIO(rollout), _codex_boundary("msg_absent"),
    ) is None
    assert codex_rollout_boundary_prefix_bytes(
        BytesIO(rollout), _codex_boundary(None),
    ) == len(rollout) - len(_line({"type": "event_msg", "payload": {"type": "task_complete"}}))


def test_reconstructed_transcript_still_proves_an_id_carrying_row_by_row_uuid() -> None:
    """After a cold resume or harness switch CORSIA wrote the transcript itself."""
    from core.fork_cc_boundary_records import boundary_prefix_from_stream
    from history.types import DurableBoundaryMessage
    from subagents.cc_transcript_projection import _assistant_message_id, _stable_uuid

    row_uuid = _stable_uuid("resumed-session", "corsia-row", "assistant")
    transcript = b"".join((
        _line({"type": "user", "uuid": _stable_uuid("resumed-session", "u", "user"),
               "message": {"role": "user", "content": "please fix it"}}),
        _line({"type": "assistant", "uuid": row_uuid,
               "message": {"id": _assistant_message_id(row_uuid), "role": "assistant",
                           "content": [{"type": "text", "text": "Fixed."}]}}),
    ))
    boundary = DurableBoundaryMessage(
        role="assistant", content="Fixed.", message_id="corsia-row",
        assistant_step_id="corsia-row",
        # A Codex-origin row replayed into Claude keeps its Codex item id.
        provider_message_id="msg_031585a640abb93a016ab17415255487d0a456247b78a06556",
    )

    assert boundary_prefix_from_stream(
        BytesIO(transcript), boundary, "resumed-session",
    ) == len(transcript)


@pytest.mark.asyncio
async def test_unavailable_target_is_its_own_idle_refusal(
    isolated_db, monkeypatch,
) -> None:
    from llm.resolution import ModelUnavailableError

    thread_id = "cc-no-target"
    _seed_settled_turn(isolated_db, thread_id, "claude-code/test", "cc", "msg_01Final")
    _launched, events = _patch_idle_environment(monkeypatch, None, "claude")

    def unavailable(_model, db=None):
        raise ModelUnavailableError("claude-code/test", "not connected")

    monkeypatch.setattr("llm.resolution.resolve_execution_target", unavailable)

    await _fire(thread_id)

    assert events == [(
        "refused", {"thread_id": thread_id, "properties": {"reason": "target_unavailable"}},
    )]


@pytest.mark.asyncio
async def test_unproven_warm_capture_is_its_own_idle_refusal(
    isolated_db, tmp_path, monkeypatch,
) -> None:
    thread_id = "codex-unproven"
    row_id_only = "msg_031585a640abb93a016ab17445544887d09acabf41d1773ab3"
    _seed_settled_turn(isolated_db, thread_id, "codex/test", "codex", "msg_not_in_rollout")
    rollout = _codex_rollout(tmp_path / "sessions" / "2026" / "09" / "21", row_id_only)
    monkeypatch.setattr(
        "core.fork_codex._registered_rollout",
        lambda tid: RegisteredRollout(PROVIDER_THREAD_ID, rollout, "high", None),
    )
    launched, events = _patch_idle_environment(
        monkeypatch, _target("codex", "codex/test"), "codex",
    )

    await _fire(thread_id)

    assert launched == []
    assert events == [(
        "refused", {"thread_id": thread_id, "properties": {"reason": "warm_capture_refused"}},
    )]
    assert _marker_pending(isolated_db) == 0


def test_codex_agent_message_event_carries_the_item_id() -> None:
    from subagents.codex_app_server_events import item_completed_event

    event = item_completed_event(
        {"type": "agentMessage", "id": "msg_item", "text": "Fixed."},
        provider_thread_id=PROVIDER_THREAD_ID, provider_turn_id="turn-p",
    )
    assert event is not None
    assert event["provider_message_id"] == "msg_item"
    assert SimpleNamespace(**event).kind == "assistant_text"
