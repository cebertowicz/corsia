"""Regressions for display corruption and explicit delivery retry revival.

Each test reproduces a production failure observed that day; the docstring
names the observable symptom the fix removes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest


# --- display: one bad row must never 500 a thread -------------------------


def _assistant_row(step_id: str = "step-1") -> dict:
    return {
        "id": step_id,
        "thread_id": "thread-1",
        "role": "assistant",
        "content": "",
        "seq": 4,
        "metadata": json.dumps({
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ]
        }),
    }


def _provisional_tool_row(step_id: str = "step-1") -> dict:
    """A real tool result awaiting its provider call id — no tool_call_id."""
    return {
        "id": "tool-provisional",
        "thread_id": "thread-1",
        "role": "tool",
        "content": "file contents",
        "seq": 5,
        "metadata": json.dumps({
            "assistant_step_id": step_id,
            "tool_invocation_id": "invocation-1",
            "tool_result_binding_state": "provisional",
            "name": "read_file",
            "arguments": {"path": "x"},
        }),
    }


def test_display_renders_a_thread_containing_a_provisional_tool_result() -> None:
    """A provisional tool result made GET /messages 500 the whole thread.

    Observed on a brand-new thread after its first tool call: the result was
    written before binding, the renderer required ``tool_call_id``, and one
    row out of thousands made the thread unopenable.
    """
    from core.message_display import llm_to_display

    rows = [
        {"id": "u1", "thread_id": "thread-1", "role": "user",
         "content": "hi", "seq": 1, "metadata": None},
        _assistant_row(),
        _provisional_tool_row(),
    ]

    display = llm_to_display(rows)

    roles = [row.get("role") for row in display]
    assert "user" in roles
    assert "assistant" in roles  # the thread renders; only linkage degrades


def test_display_renders_despite_duplicate_assistant_step_ids() -> None:
    """An index-level lineage anomaly must degrade, not 500 the thread."""
    from core.message_display import llm_to_display

    duplicate = dict(_assistant_row(), seq=6)
    rows = [_assistant_row(), duplicate, _provisional_tool_row()]

    display = llm_to_display(rows)

    assert [row.get("role") for row in display] == ["assistant", "assistant"]


def _insert_session(thread_id: str = "thread-1") -> None:
    from infra.database import get_db

    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (thread_id, "user-1", "Thread", "claude-code/sonnet", now, now),
        )
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,"
            "status,started_at,ended_at,boot_epoch) VALUES "
            "('turn-0',?,0,'claude-code/sonnet','completed','t0','t0','test')",
            (thread_id,),
        )
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq,"
            "metadata) VALUES ('sealed-message',?,'user','before retry',"
            "'t0',1,?)",
            (thread_id, json.dumps({"turn_id": "turn-0"})),
        )
        db.commit()
    finally:
        db.close()


# --- retry: a blocked message must be recoverable -------------------------


def _insert_ambiguous_message(
    thread_id: str = "thread-1",
    message_id: str = "blocked-1",
    delivery_error: str = "provider_write_outcome_unknown",
) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq,"
            "delivery_state,delivery_error,write_outcome) VALUES "
            "(?,?,'user',?,'t1',2,'ambiguous',?,'ambiguous')",
            (message_id, thread_id, "please deliver me", delivery_error),
        )
        db.commit()
    finally:
        db.close()


def test_retry_drops_the_ended_attempts_turn_and_input_identity() -> None:
    """A retried row must be a new Turn input and a distinct UI message."""
    from api.thread_inbox_actions import _retry_content

    content, metadata = _retry_content({
        "id": "blocked-1",
        "content": "please deliver me",
        "delivery_state": "failed",
        "metadata": json.dumps({
            "turn_id": "ended-turn",
            "input_id": "ended-input",
            "user_annotation": "preserve me",
        }),
    })

    assert content == "please deliver me"
    assert metadata == {
        "user_annotation": "preserve me",
        "retried_from_message_id": "blocked-1",
    }


@pytest.mark.asyncio
async def test_retry_revives_a_blocked_native_message(
    isolated_db, monkeypatch,
) -> None:
    """Retry on an ended message was a guaranteed 409.

    Retry re-sends an ambiguous write through normal admission while retaining
    the original row as the audit record of the uncertain provider write.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import thread_inbox_actions
    from infra.database import get_db

    _insert_session("native-thread")
    db = get_db()
    try:
        db.execute(
            "UPDATE threads SET model='oMLX/qwen' WHERE id='native-thread'",
        )
        db.commit()
    finally:
        db.close()
    _insert_ambiguous_message("native-thread")

    sent = AsyncMock(
        return_value=SimpleNamespace(message_id="revived-1", driver="native"),
    )
    import infra.send_service as send_service

    monkeypatch.setattr(send_service, "send_thread_input", sent)

    response = await thread_inbox_actions.retry_thread_delivery_now(
        "native-thread", "blocked-1", "user-1",
    )

    sent.assert_awaited_once()
    call = sent.await_args
    assert call is not None
    assert call.args[2].endswith("please deliver me")
    assert "prior attempt may have reached the model" in call.args[2]
    assert call.kwargs["metadata"] == {
        "retried_from_message_id": "blocked-1",
        "possible_duplicate": True,
    }
    assert call.kwargs["native_mode"] == "event"
    assert "is_retry" not in call.kwargs

    assert response["retried"] is True
    assert response["revived_message_id"] == "revived-1"

    db = get_db()
    try:
        row = db.execute(
            "SELECT delivery_state,delivery_error,seq FROM messages "
            "WHERE id='blocked-1'",
        ).fetchone()
    finally:
        db.close()
    # Retry creates a new ordinary input. The original remains the immutable
    # audit record of the ambiguous write instead of gaining a second lifecycle.
    assert row["delivery_state"] == "ambiguous"
    assert row["delivery_error"] == "provider_write_outcome_unknown"


@pytest.mark.asyncio
async def test_failed_revival_restores_the_retry_control(
    isolated_db, monkeypatch,
) -> None:
    """A rejected revival send must put the original error (and Retry) back."""
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from api import thread_inbox_actions
    from infra.database import get_db
    from infra.send_types import SendRejected

    _insert_session("native-thread-3")
    db = get_db()
    try:
        db.execute(
            "UPDATE threads SET model='oMLX/qwen' WHERE id='native-thread-3'",
        )
        db.commit()
    finally:
        db.close()
    _insert_ambiguous_message("native-thread-3", "blocked-3")

    sent = AsyncMock(side_effect=SendRejected(409, "send admission refused"))
    import infra.send_service as send_service

    monkeypatch.setattr(send_service, "send_thread_input", sent)

    with pytest.raises(HTTPException) as excinfo:
        await thread_inbox_actions.retry_thread_delivery_now(
            "native-thread-3", "blocked-3", "user-1",
        )
    assert excinfo.value.status_code == 409

    db = get_db()
    try:
        row = db.execute(
            "SELECT delivery_state,delivery_error FROM messages "
            "WHERE id='blocked-3'",
        ).fetchone()
    finally:
        db.close()
    assert row["delivery_state"] == "ambiguous"
    assert row["delivery_error"] == "provider_write_outcome_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model", ["claude-code/sonnet", "codex/gpt-5.6-sol", "oMLX/qwen"],
)
async def test_retry_revives_a_blocked_message_on_every_harness(
    isolated_db, monkeypatch, model,
) -> None:
    """Retry is one click on Claude Code and codex too, not just native.

    This cell used to assert the opposite ("revival is native-only; external
    harnesses keep the explicit 409"). That made the red `!` and its Retry a
    DEAD control on exactly the threads the owner hits this on: the client
    offers Retry on every ambiguous message, and the endpoint
    answered "already ended… send it again" for two of the three harnesses.
    Observed in production on a `codex/gpt-5.6-sol` thread.

    Nothing justified the narrowing. Revival re-sends the content through
    `send_thread_input`, which dispatches on `execution_class` like every other
    send, and `native_mode="event"` is the harness-agnostic mode events already
    use to reach cc and codex threads.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import thread_inbox_actions
    from infra.database import get_db
    import infra.send_service as send_service

    _insert_session("retry-thread")
    db = get_db()
    try:
        db.execute("UPDATE threads SET model=? WHERE id='retry-thread'", (model,))
        db.commit()
    finally:
        db.close()
    _insert_ambiguous_message("retry-thread", "blocked-x")

    sent = AsyncMock(return_value=SimpleNamespace(message_id="revived-x"))
    monkeypatch.setattr(send_service, "send_thread_input", sent)

    response = await thread_inbox_actions.retry_thread_delivery_now(
        "retry-thread", "blocked-x", "user-1",
    )

    sent.assert_awaited_once()
    assert sent.await_args.kwargs["native_mode"] == "event"
    assert response["retried"] is True
    assert response["revived_message_id"] == "revived-x"


@pytest.mark.asyncio
async def test_retry_resends_a_failed_input_verbatim(isolated_db, monkeypatch) -> None:
    """Retry resends the stored input exactly, edge whitespace included.

    Live sends store content untrimmed, so re-trimming on Retry would deliver
    a different input than the one that failed.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import thread_inbox_actions
    from infra.database import get_db
    import infra.send_service as send_service

    content = "  keep my edges \n"
    _insert_session("verbatim-thread")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq,"
            "delivery_state,delivery_error,write_outcome) VALUES "
            "('failed-1','verbatim-thread','user',?,'t1',2,'failed',"
            "'refused','proved_no_write')",
            (content,),
        )
        db.commit()
    finally:
        db.close()

    sent = AsyncMock(return_value=SimpleNamespace(message_id="revived-v"))
    monkeypatch.setattr(send_service, "send_thread_input", sent)

    await thread_inbox_actions.retry_thread_delivery_now(
        "verbatim-thread", "failed-1", "user-1",
    )

    call = sent.await_args
    assert call is not None
    assert call.args[2] == content
    assert call.kwargs["trim_content"] is False
