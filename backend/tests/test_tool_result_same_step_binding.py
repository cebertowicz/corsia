from __future__ import annotations

import json

import pytest


class _Stdin:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    def write(self, payload: bytes) -> None:
        self.frames.append(json.loads(payload))

    async def drain(self) -> None:
        return None


class _Proc:
    def __init__(self) -> None:
        self.stdin = _Stdin()
        self.returncode = None


def _seed_thread(db, thread_id: str = "thread-1") -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?,?,?,'t','t')",
        (thread_id, "user-1", "codex/gpt"),
    )
    db.commit()


async def _declare_late_call(
    session, *, request_id: int, call_id: str, provider_thread_id: str,
    provider_turn_id: str,
) -> None:
    await session._handle_message({
        "id": request_id,
        "method": "item/tool/call",
        "params": {
            "threadId": provider_thread_id,
            "turnId": provider_turn_id,
            "callId": call_id,
            "tool": "plan_update",
            "arguments": {},
        },
    })
    await session.wait_dynamic_tool_requests_idle()
    await session._handle_notification("item/started", {
        "threadId": provider_thread_id,
        "turnId": provider_turn_id,
        "item": {
            "id": call_id,
            "type": "dynamicToolCall",
            "tool": "plan_update",
            "arguments": {},
        },
    })


async def _complete_late_call(
    session, *, call_id: str, provider_thread_id: str, provider_turn_id: str,
) -> None:
    await session._handle_notification("item/completed", {
        "threadId": provider_thread_id,
        "turnId": provider_turn_id,
        "item": {
            "id": call_id,
            "type": "dynamicToolCall",
            "result": "rejected by bridge",
            "success": False,
        },
    })


def test_bound_sibling_refuses_declared_undispatched_echo_in_same_step(
    isolated_db,
) -> None:
    """A bound sibling cannot prove another declared echo was undispatched."""
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.tool_result_binding import bind_tool_result_echo
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use

    db = get_db()
    _seed_thread(db)
    step_id = persist_tool_use(
        "thread-1", turn_id="turn-1", provider_turn_id="provider-turn-1",
        tool_id="first-call", name="write_file", arguments={"path": "one"}, db=db,
    )
    first = bind_tool_result_echo(
        thread_id="thread-1", provider_tool_call_id="first-call",
        assistant_step_id=step_id, echoed_content="bridge rejected before execution", db=db,
    )
    assert first.provider_tool_call_id == "first-call"
    assert persist_tool_use(
        "thread-1", turn_id="turn-1", provider_turn_id="provider-turn-1",
        tool_id="second-call", name="write_file", arguments={"path": "two"}, db=db,
        assistant_step_id=step_id,
    ) == step_id

    with pytest.raises(ToolResultBindingError, match="does not identify one canonical result"):
        bind_tool_result_echo(
            thread_id="thread-1", provider_tool_call_id="second-call",
            assistant_step_id=step_id, echoed_content="bridge rejected before execution", db=db,
        )
    rows = db.execute(
        "SELECT metadata FROM messages WHERE thread_id='thread-1' AND role='tool'"
    ).fetchall()
    assert {json.loads(row["metadata"])["tool_call_id"] for row in rows} == {"first-call"}
    db.close()


def test_provisional_sibling_still_refuses_undeclared_echo(isolated_db) -> None:
    """An unbound sibling remains unsafe evidence for fallback."""
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.tool_result_delivery import persist_tool_execution_result
    from core.tool_result_binding import bind_tool_result_echo
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use

    db = get_db()
    _seed_thread(db)
    step_id = persist_tool_use(
        "thread-1", turn_id="turn-1", provider_turn_id="provider-turn-1",
        tool_id="first-call", name="write_file", arguments={"path": "one"}, db=db,
    )
    persist_tool_execution_result(
        thread_id="thread-1", turn_id="turn-1", invocation_id="first-invocation",
        assistant_step_id=step_id, name="write_file", arguments={"path": "one"},
        content="possible execution", is_error=False, db=db,
    )
    assert persist_tool_use(
        "thread-1", turn_id="turn-1", provider_turn_id="provider-turn-1",
        tool_id="second-call", name="write_file", arguments={"path": "two"}, db=db,
        assistant_step_id=step_id,
    ) == step_id

    with pytest.raises(ToolResultBindingError, match="does not identify one canonical result"):
        bind_tool_result_echo(
            thread_id="thread-1", provider_tool_call_id="second-call",
            assistant_step_id=step_id, echoed_content="bridge rejected before execution", db=db,
        )
    db.close()


def test_unknown_invocation_marker_still_refuses_same_step_fallback(isolated_db) -> None:
    """A provenance marker that identifies no canonical row is not an orphan echo."""
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.tool_result_binding import bind_tool_result_echo
    from core.tool_result_invocation import with_invocation_reference
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use

    db = get_db()
    _seed_thread(db)
    step_id = persist_tool_use(
        "thread-1", turn_id="turn-1", provider_turn_id="provider-turn-1",
        tool_id="first-call", name="write_file", arguments={"path": "one"}, db=db,
    )
    bind_tool_result_echo(
        thread_id="thread-1", provider_tool_call_id="first-call",
        assistant_step_id=step_id, echoed_content="bridge rejected before execution", db=db,
    )
    assert persist_tool_use(
        "thread-1", turn_id="turn-1", provider_turn_id="provider-turn-1",
        tool_id="second-call", name="write_file", arguments={"path": "two"}, db=db,
        assistant_step_id=step_id,
    ) == step_id

    with pytest.raises(ToolResultBindingError, match="does not identify one canonical result"):
        bind_tool_result_echo(
            thread_id="thread-1", provider_tool_call_id="second-call",
            assistant_step_id=step_id,
            echoed_content=with_invocation_reference("forged", "unknown-invocation"),
            db=db,
        )
    db.close()


@pytest.mark.parametrize(
    ("tool_call_id", "provider_tool_call_id"),
    [
        (42, 42),
        (["other"], ["other"]),
        ({"id": "other"}, {"id": "other"}),
        ("   ", "   "),
        ("other-call", "contradictory-call"),
    ],
)
def test_malformed_or_contradictory_bound_sibling_refuses_fallback(
    isolated_db, tool_call_id, provider_tool_call_id,
) -> None:
    """Only matching nonblank raw string identities prove a sibling is other."""
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.tool_result_binding import bind_tool_result_echo
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use

    db = get_db()
    _seed_thread(db)
    step_id = persist_tool_use(
        "thread-1", turn_id="turn-1", provider_turn_id="provider-turn-1",
        tool_id="first-call", name="write_file", arguments={"path": "one"}, db=db,
    )
    first = bind_tool_result_echo(
        thread_id="thread-1", provider_tool_call_id="first-call",
        assistant_step_id=step_id, echoed_content="bridge rejected before execution", db=db,
    )
    row = db.execute("SELECT metadata FROM messages WHERE id=?", (first.message_id,)).fetchone()
    metadata = json.loads(row["metadata"])
    metadata["tool_call_id"] = tool_call_id
    metadata["provider_tool_call_id"] = provider_tool_call_id
    db.execute("UPDATE messages SET metadata=? WHERE id=?", (json.dumps(metadata), first.message_id))
    assert persist_tool_use(
        "thread-1", turn_id="turn-1", provider_turn_id="provider-turn-1",
        tool_id="second-call", name="write_file", arguments={"path": "two"}, db=db,
        assistant_step_id=step_id,
    ) == step_id

    with pytest.raises(ToolResultBindingError, match="does not identify one canonical result"):
        bind_tool_result_echo(
            thread_id="thread-1", provider_tool_call_id="second-call",
            assistant_step_id=step_id, echoed_content="bridge rejected before execution", db=db,
        )
    db.close()


@pytest.mark.asyncio
async def test_codex_old_turn_completion_is_ignored_after_restart(isolated_db, monkeypatch):
    from codex_runtime_fixtures import _admit_active_turn
    from infra.database import get_db
    from shared_send_fixtures import _insert_session
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_runtime_event_delivery import runtime_event_handler
    from subagents.codex_runtime_state import _LiveCodex

    thread_id = "restart-thread"
    _insert_session(thread_id, model="codex/gpt-5-codex", spawned_by=None)
    identity = _admit_active_turn(thread_id, turn_id="restart-turn")
    session = CodexAppServerSession(
        binary="unused", task=None, codex_thread_id="restart-provider",
        corsia_thread_id=thread_id, corsia_turn_identity=identity, tool_registry=object(),
    )
    session._track_active_turn("new-provider-turn")
    assert session._tool_requests is not None

    live = _LiveCodex(
        codex_thread_id="restart-provider", session=session, parent_thread_id=None,
        name="codex", corsia_turn=identity,
    )
    live.bind_app_server_session(session)
    def unexpected_binding(**_kwargs):
        pytest.fail("old-turn echo reached binding")
    monkeypatch.setattr("core.tool_result_binding.bind_tool_result_echo", unexpected_binding)
    session.set_callbacks(on_event=runtime_event_handler(live, thread_id, lambda *_: None))
    await _complete_late_call(
        session, call_id="old-call", provider_thread_id="restart-provider",
        provider_turn_id="old-provider-turn",
    )

    db = get_db()
    try:
        assert db.execute(
            "SELECT count(*) FROM messages WHERE thread_id=? AND role='tool'", (thread_id,),
        ).fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_codex_pre_execution_rejections_settle_on_separate_steps(
    isolated_db,
) -> None:
    """Callbacks refused before execution settle as undispatched on separate steps."""
    from codex_runtime_fixtures import _admit_active_turn
    from shared_send_fixtures import _insert_session
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_runtime_event_delivery import runtime_event_handler
    from subagents.codex_runtime_state import _LiveCodex

    thread_id = "race-thread"
    provider_thread_id = "race-provider"
    provider_turn_id = "race-provider-turn"
    call_ids = ("first-call", "second-call")
    _insert_session(thread_id, model="codex/gpt-5-codex", spawned_by=None)
    identity = _admit_active_turn(thread_id, turn_id="race-turn")
    session = CodexAppServerSession(
        binary="unused", task=None, codex_thread_id=provider_thread_id,
        corsia_thread_id=thread_id, corsia_turn_identity=identity, tool_registry=object(),
    )
    session._proc = _Proc()
    session._track_active_turn(provider_turn_id)
    live = _LiveCodex(
        codex_thread_id=provider_thread_id, session=session, parent_thread_id=None,
        name="codex", corsia_turn=identity,
    )
    live.bind_app_server_session(session)
    session.set_callbacks(on_event=runtime_event_handler(live, thread_id, lambda *_: None))

    async def receipt(_frame: dict) -> dict:
        return {"last": {"inputTokens": 10, "outputTokens": 1}}

    assert session._tool_requests is not None
    session._tool_requests._usage = receipt
    await _declare_late_call(
        session, request_id=1, call_id=call_ids[0],
        provider_thread_id=provider_thread_id, provider_turn_id=provider_turn_id,
    )
    first_step_id = live.tool_step_ids[call_ids[0]]
    await _complete_late_call(
        session, call_id=call_ids[0], provider_thread_id=provider_thread_id,
        provider_turn_id=provider_turn_id,
    )
    assert live.tool_step_ids == {}

    await _declare_late_call(
        session, request_id=2, call_id=call_ids[1],
        provider_thread_id=provider_thread_id, provider_turn_id=provider_turn_id,
    )
    second_step_id = live.tool_step_ids[call_ids[1]]
    assert second_step_id != first_step_id

    await _complete_late_call(
        session, call_id=call_ids[1], provider_thread_id=provider_thread_id,
        provider_turn_id=provider_turn_id,
    )
    assert live.tool_step_ids == {}

    from infra.database import get_db

    db = get_db()
    try:
        rows = db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND role='tool'", (thread_id,),
        ).fetchall()
    finally:
        db.close()
    assert {json.loads(row["metadata"])["tool_call_id"] for row in rows} == set(call_ids)
    assert {json.loads(row["metadata"])["assistant_step_id"] for row in rows} == {
        first_step_id, second_step_id,
    }



@pytest.mark.asyncio
async def test_codex_call_declared_behind_input_settlement_binds_its_step(
    isolated_db, monkeypatch,
) -> None:
    """A request waits for the declaration it followed, even behind the input gate."""
    import asyncio

    from codex_runtime_fixtures import _admit_active_turn
    from shared_send_fixtures import _insert_session
    from subagents import harness_tool_bridge
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_runtime_event_delivery import (
        arm_delivery_event_barrier,
        release_delivery_event_barrier,
        runtime_event_handler,
    )
    from subagents.codex_runtime_state import _LiveCodex

    thread_id = "held-thread"
    provider_thread_id = "held-provider"
    provider_turn_id = "held-provider-turn"
    call_id = "held-call"
    _insert_session(thread_id, model="codex/gpt-5-codex", spawned_by=None)
    identity = _admit_active_turn(thread_id, turn_id="held-turn")
    session = CodexAppServerSession(
        binary="unused", task=None, codex_thread_id=provider_thread_id,
        corsia_thread_id=thread_id, corsia_turn_identity=identity, tool_registry=object(),
    )
    session._proc = _Proc()
    session._track_active_turn(provider_turn_id)
    live = _LiveCodex(
        codex_thread_id=provider_thread_id, session=session, parent_thread_id=None,
        name="codex", corsia_turn=identity,
    )
    live.bind_app_server_session(session)
    session.set_callbacks(on_event=runtime_event_handler(live, thread_id, lambda *_: None))
    executed: list[str | None] = []

    async def execute_tool(_registry, **kwargs) -> str:
        executed.append(kwargs["assistant_step_id"])
        return "held payload"

    async def receipt(_frame: dict) -> dict:
        return {"last": {"inputTokens": 10, "outputTokens": 1}}

    monkeypatch.setattr(harness_tool_bridge, "execute_tool", execute_tool)
    assert session._tool_requests is not None
    session._tool_requests._usage = receipt
    # turn/start was accepted; its input settlement still holds provider events.
    arm_delivery_event_barrier(live)
    await session._handle_notification("item/started", {
        "threadId": provider_thread_id,
        "turnId": provider_turn_id,
        "item": {"id": call_id, "type": "dynamicToolCall", "tool": "read_file", "arguments": {}},
    })
    await session._handle_message({
        "id": 1,
        "method": "item/tool/call",
        "params": {
            "threadId": provider_thread_id, "turnId": provider_turn_id,
            "callId": call_id, "tool": "read_file", "arguments": {},
        },
    })
    request = next(iter(session._tool_requests.tasks))
    answered, _pending = await asyncio.wait({request}, timeout=0.2)
    assert not answered and session._proc.stdin.frames == []

    await release_delivery_event_barrier(live)
    await session.wait_dynamic_tool_requests_idle()
    assert executed == [live.tool_step_ids[call_id]]
    assert [frame["id"] for frame in session._proc.stdin.frames] == [1]
    assert "held payload" in json.dumps(session._proc.stdin.frames[0]["result"])
