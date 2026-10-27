from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _seed_thread(db, *, turn_id: str = "turn-1"):
    from core.turn_core import BOOT_EPOCH
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','claude-code/sonnet','t','t')"
    )
    db.commit()
    admission = admit_input(
        "thread-1",
        role="user",
        content="run tools",
        intent="apply_now",
        message_id=f"{turn_id}:input",
        turn_id=turn_id,
        boot_epoch=BOOT_EPOCH,
        admitted_at="t",
        db=db,
    )
    assert admission.batch is not None
    begin_batch_write(
        admission.batch,
        attempt_id=f"{turn_id}:write",
        started_at="t",
        db=db,
    )
    settle_batch_write(f"{turn_id}:write", outcome="accepted", db=db)
    return admission.batch.identity


def _tool_use(tool_id: str, path: str) -> dict:
    return {
        "kind": "assistant_step",
        "text": "",
        "reasoning": "",
        "tool_uses": [{
            "id": tool_id,
            "name": "mcp__corsia__read_file",
            "input": {"path": path},
        }],
    }


@pytest.mark.asyncio
async def test_cc_bridge_binds_execution_to_latest_assistant_step(
    isolated_db,
) -> None:
    """A prior pending echo must not make the next real tool lack a durable row."""
    from api import cc_attach
    from infra.database import get_db
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _LIVE, _Live

    class Registry:
        def get(self, _name):
            return None

        async def execute(self, _name: str, arguments: dict) -> str:
            return f"contents for {arguments['path']}"

    db = get_db()
    identity = _seed_thread(db)
    live = _Live(
        session=SimpleNamespace(record_tool_reply=AsyncMock(), tool_context=SimpleNamespace(tokens=100)),
        parent_thread_id=None,
        name="cc",
        corsia_turn=identity,
        tool_registry=Registry(),
        turn_active=True,
        last_call_usage={"input_tokens": 100},
        context_window=100_000,
        catalog_model_id="claude-code/sonnet",
    )
    _LIVE.put("thread-1", live)
    on_event = _make_on_event(live, "thread-1", lambda *_args: None, db)

    await on_event(_tool_use("provider-old", "old.md"))
    old_step = live.last_assistant_message_id
    await on_event(_tool_use("provider-latest", "latest.md"))
    latest_step = live.last_assistant_message_id
    assert old_step and latest_step and old_step != latest_step

    projection = await cc_attach._execute_admitted_tool(
        live,
        "thread-1",
        {
            "invocation_id": "invocation-latest",
            "provider_tool_call_id": "provider-latest",
            "name": "read_file",
            "arguments": {"path": "latest.md"},
        },
    )

    rows = db.execute(
        "SELECT content,metadata FROM messages "
        "WHERE thread_id='thread-1' AND role='tool'"
    ).fetchall()
    assert len(rows) == 1
    metadata = json.loads(rows[0]["metadata"])
    assert rows[0]["content"] == "contents for latest.md"
    assert metadata["assistant_step_id"] == latest_step
    assert metadata["tool_invocation_id"] == "invocation-latest"
    assert metadata["tool_call_id"] == "provider-latest"
    assert metadata["tool_result_binding_state"] == "bound"

    await on_event({
        "kind": "tool_result",
        "tool_use_id": "provider-latest",
        "content": projection,
        "is_error": False,
    })
    bound = json.loads(db.execute(
        "SELECT metadata FROM messages WHERE thread_id='thread-1' "
        "AND role='tool'"
    ).fetchone()["metadata"])
    assert bound["assistant_step_id"] == latest_step
    assert bound["tool_call_id"] == "provider-latest"
    assert bound["tool_result_binding_state"] == "bound"
    assert live.tool_step_ids == {"provider-old": old_step}

    _LIVE.pop("thread-1", None)
    db.close()


@pytest.mark.asyncio
async def test_cc_bridge_rejects_substituted_and_replayed_tool_call(
    isolated_db, monkeypatch,
) -> None:
    """A loopback caller cannot replace a declared call or execute it twice."""
    from api import cc_attach
    from subagents import cc_runtime_quiescence

    monkeypatch.setattr(cc_runtime_quiescence, "fence_tool_failure", lambda *_args: False)
    from core._tool_result_delivery_support import ToolResultBindingError
    from infra.database import get_db
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _LIVE, _Live

    registry = SimpleNamespace(
        get=lambda _name: None,
        execute=AsyncMock(return_value="contents"),
    )
    db = get_db()
    identity = _seed_thread(db)
    live = _Live(
        session=SimpleNamespace(record_tool_reply=AsyncMock(), tool_context=SimpleNamespace(tokens=100)),
        parent_thread_id=None, name="cc", corsia_turn=identity,
        tool_registry=registry, turn_active=True,
        last_call_usage={"input_tokens": 100}, context_window=100_000,
        catalog_model_id="claude-code/sonnet",
    )
    _LIVE.put("thread-1", live)
    try:
        on_event = _make_on_event(live, "thread-1", lambda *_args: None, db)
        await on_event(_tool_use("provider-call", "allowed.md"))
        body = {
            "invocation_id": "invocation-1", "provider_tool_call_id": "provider-call",
            "name": "read_file", "arguments": {"path": "allowed.md"},
        }
        with pytest.raises(ToolResultBindingError):
            await cc_attach._execute_admitted_tool(
                live, "thread-1", body | {"name": "delegate_send"},
            )
        with pytest.raises(ToolResultBindingError):
            await cc_attach._execute_admitted_tool(
                live, "thread-1", body | {"arguments": {"path": "forged.md"}},
            )
        registry.execute.assert_not_awaited()
        await cc_attach._execute_admitted_tool(live, "thread-1", body)
        registry.execute.assert_awaited_once()
        # The successful call consumed its step; expire the missing-step wait
        # immediately while retaining the replay refusal path.
        monkeypatch.setattr(cc_attach, "_SPAWN_LOCK_TIMEOUT_S", 0)
        with pytest.raises(ToolResultBindingError):
            await cc_attach._execute_admitted_tool(live, "thread-1", body)
        registry.execute.assert_awaited_once()
    finally:
        _LIVE.pop("thread-1", None)
        db.close()


@pytest.mark.asyncio
async def test_cc_delayed_echo_uses_its_durable_assistant_step(isolated_db) -> None:
    """A later Assistant Step cannot orphan an already-bound result echo."""
    from api import cc_attach
    from core.tool_result_binding import read_bound_tool_result
    from infra.database import get_db
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _LIVE, _Live
    from subagents.child_thread import load_assistant_step_display

    class Registry:
        def get(self, _name):
            return None

        async def execute(self, _name: str, arguments: dict) -> str:
            return f"contents for {arguments['path']}"

    db = get_db()
    identity = _seed_thread(db)
    live = _Live(
        session=SimpleNamespace(record_tool_reply=AsyncMock(), tool_context=SimpleNamespace(tokens=100)),
        parent_thread_id=None,
        name="cc",
        corsia_turn=identity,
        tool_registry=Registry(),
        turn_active=True,
        last_call_usage={"input_tokens": 100},
        context_window=100_000,
        catalog_model_id="claude-code/sonnet",
    )
    _LIVE.put("thread-1", live)
    on_event = _make_on_event(live, "thread-1", lambda *_args: None, db)

    await on_event(_tool_use("provider-first", "first.md"))
    first_step = live.last_assistant_message_id
    projection = await cc_attach._execute_admitted_tool(
        live,
        "thread-1",
        {
            "invocation_id": "invocation-first",
            "provider_tool_call_id": "provider-first",
            "name": "read_file",
            "arguments": {"path": "first.md"},
        },
    )
    assert "provider-first" not in live.tool_step_ids

    await on_event(_tool_use("provider-second", "second.md"))
    second_step = live.last_assistant_message_id
    assert first_step and second_step and first_step != second_step

    await on_event({
        "kind": "tool_result",
        "tool_use_id": "provider-first",
        "content": projection,
        "is_error": False,
    })

    bound = read_bound_tool_result(
        thread_id="thread-1",
        provider_tool_call_id="provider-first",
        db=db,
    )
    first_projection = load_assistant_step_display(
        "thread-1", first_step, db=db,
    )
    second_projection = load_assistant_step_display(
        "thread-1", second_step, db=db,
    )
    first_calls = first_projection["metadata"]["tool_calls"]
    second_calls = second_projection["metadata"]["tool_calls"]
    assert bound is not None and bound.assistant_step_id == first_step
    assert [(call["id"], call.get("result")) for call in first_calls] == [
        ("provider-first", "contents for first.md"),
    ]
    assert [call["id"] for call in second_calls] == ["provider-second"]
    assert live.tool_step_ids == {"provider-second": second_step}
    _LIVE.pop("thread-1", None)
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outstanding_steps",
    [
        {},
        {"provider-stale-a": "stale-step-a", "provider-stale-b": "stale-step-b"},
    ],
    ids=["empty-outstanding-map", "stale-ambiguous-map"],
)
async def test_cc_bridge_rejects_uncorroborated_latest_step_before_execution(
    isolated_db,
    outstanding_steps: dict[str, str],
) -> None:
    """A latest-step cache value cannot authorize a stale or duplicate execution."""
    from api import cc_attach
    from core._tool_result_delivery_support import ToolResultBindingError
    from infra.database import get_db
    from subagents.cc_runtime_state import _Live
    from subagents.child_thread import persist_tool_use

    class Registry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def get(self, _name):
            return None

        async def execute(self, name: str, arguments: dict) -> str:
            self.calls.append((name, arguments))
            return "must not execute"

    db = get_db()
    identity = _seed_thread(db)
    latest_step = persist_tool_use(
        "thread-1",
        turn_id=identity.turn_id,
        tool_id="provider-latest",
        name="mcp__corsia__read_file",
        arguments={"path": "latest.md"},
        db=db,
    )
    registry = Registry()
    live = _Live(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="cc",
        corsia_turn=identity,
        tool_registry=registry,
        turn_active=True,
        last_assistant_message_id=latest_step,
        tool_step_ids=dict(outstanding_steps),
    )

    with pytest.raises(
        ToolResultBindingError,
        match="lacks its exact provider Assistant Step",
    ):
        await cc_attach._execute_admitted_tool(
            live,
            "thread-1",
            {
                "invocation_id": "invocation-latest",
                "name": "read_file",
                "arguments": {"path": "latest.md"},
            },
        )

    assert registry.calls == []
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='thread-1' AND role='tool'"
    ).fetchone()[0] == 0
    db.close()


@pytest.mark.asyncio
async def test_cc_bridge_waits_for_durable_assistant_step_before_execution(
    isolated_db,
    monkeypatch,
) -> None:
    """An MCP-first call executes once only after its CC Step is durable."""
    import asyncio
    import sqlite3

    from api import cc_attach
    from infra.database import get_db
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live

    db = get_db()
    identity = _seed_thread(db)

    class Registry:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, _name):
            return None

        async def execute(self, _name: str, _arguments: dict) -> str:
            with sqlite3.connect(isolated_db) as durable_db:
                rows = durable_db.execute(
                    "SELECT metadata FROM messages WHERE thread_id='thread-1' "
                    "AND role='assistant'"
                ).fetchall()
            assert any(
                call.get("id") == "provider-race"
                for row in rows
                for call in json.loads(row[0]).get("llm_tool_calls", [])
            )
            self.calls += 1
            return "durable contents"

    registry = Registry()

    async def skip_checkpoint_boundary(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(
        "subagents.harness_checkpoint_resume.maintain_external_tool_boundary",
        skip_checkpoint_boundary,
    )
    live = _Live(
        session=SimpleNamespace(record_tool_reply=AsyncMock(), tool_context=SimpleNamespace(tokens=100)),
        parent_thread_id=None,
        name="cc",
        corsia_turn=identity,
        tool_registry=registry,
        turn_active=True,
        catalog_model_id="claude-code/sonnet",
    )
    on_event = _make_on_event(live, "thread-1", lambda *_args: None, None)
    execution = asyncio.create_task(cc_attach._execute_admitted_tool(
        live,
        "thread-1",
        {
            "invocation_id": "invocation-race",
            "provider_tool_call_id": "provider-race",
            "name": "read_file",
            "arguments": {"path": "race.md"},
        },
    ))

    await asyncio.sleep(0)
    assert not execution.done()
    assert registry.calls == 0

    await on_event(_tool_use("provider-race", "race.md"))
    result = await execution

    assert result.startswith("durable contents")
    assert registry.calls == 1
    db.close()


def test_echo_zero_candidate_binds_as_final_row_and_allows_continuation(
    isolated_db,
) -> None:
    """A missing execution row binds from its own echo and does not strand input."""
    import asyncio

    from core.tool_result_binding import bind_tool_result_echo
    from core.turn_core import BOOT_EPOCH, TurnTerminalResult, persist_turn_terminal
    from infra.database import get_db
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input
    from subagents.cc_event_tool_settlement import settle_missing_tool_results
    from subagents.cc_runtime_state import _Live
    from subagents.child_thread import persist_tool_use

    db = get_db()
    identity = _seed_thread(db)
    step_id = persist_tool_use(
        "thread-1",
        turn_id=identity.turn_id,
        tool_id="provider-missing",
        name="mcp__corsia__read_file",
        arguments={"path": "missing.md"},
        db=db,
    )
    live = _Live(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="cc",
        corsia_turn=identity,
        turn_active=True,
        tool_names={"provider-missing": "mcp__corsia__read_file"},
        tool_step_ids={"provider-missing": step_id},
    )
    bound = bind_tool_result_echo(
        thread_id="thread-1",
        provider_tool_call_id="provider-missing",
        assistant_step_id=step_id,
        echoed_content="bridge rejected before execution",
        db=db,
    )
    assert bound.is_error is True
    assert bound.content == "bridge rejected before execution"
    again = bind_tool_result_echo(
        thread_id="thread-1",
        provider_tool_call_id="provider-missing",
        assistant_step_id=step_id,
        echoed_content="bridge rejected before execution",
        db=db,
    )
    assert again.message_id == bound.message_id

    async def settle() -> None:
        await settle_missing_tool_results(
            live=live,
            child_id="thread-1",
            identity=identity,
            bcast=lambda *_args: None,
            db=db,
        )
        await settle_missing_tool_results(
            live=live,
            child_id="thread-1",
            identity=identity,
            bcast=lambda *_args: None,
            db=db,
        )

    asyncio.run(settle())
    assert live.provider_context_untrusted is True
    assert not live.tool_step_ids
    assert live.tools_settled.is_set()
    rows = db.execute(
        "SELECT metadata FROM messages WHERE thread_id='thread-1' AND role='tool'"
    ).fetchall()
    assert len(rows) == 1
    synthetic = json.loads(rows[0]["metadata"])
    assert synthetic["synthetic_reason"] == "undispatched_tool"
    assert synthetic["tool_call_id"] == "provider-missing"

    assert persist_turn_terminal(
        identity.turn_id,
        "error",
        "provider echo lacked a canonical result",
        db=db,
    ) is TurnTerminalResult.APPLIED
    receipt = admit_input(
        "thread-1",
        role="user",
        content="continue after repair",
        intent="apply_now",
        message_id="successor-input",
        turn_id="turn-2",
        boot_epoch=BOOT_EPOCH,
        admitted_at="t2",
        db=db,
    )
    repeated = admit_input(
        "thread-1",
        role="user",
        content="continue after repair",
        intent="apply_now",
        message_id="successor-input",
        turn_id="unused-duplicate-turn",
        boot_epoch=BOOT_EPOCH,
        admitted_at="t2",
        db=db,
    )
    assert receipt.batch is not None
    assert repeated.batch is None
    assert repeated.accepted.duplicate is True
    assert repeated.accepted.input_order == receipt.accepted.input_order
    begin_batch_write(
        receipt.batch,
        attempt_id="successor-write",
        started_at="t2",
        db=db,
    )
    settle_batch_write("successor-write", outcome="accepted", db=db)
    successor = receipt.batch.identity
    assert successor.turn_order == identity.turn_order + 1
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE id='successor-input'"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT delivery_state FROM messages WHERE id='successor-input'"
    ).fetchone()[0] == "delivered"
    db.close()


def test_undeclared_call_with_zero_rows_still_raises(isolated_db) -> None:
    """An echo for a call its Assistant Step never declared is still refused."""
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.tool_result_binding import bind_tool_result_echo
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use

    db = get_db()
    identity = _seed_thread(db)
    step_id = persist_tool_use(
        "thread-1",
        turn_id=identity.turn_id,
        tool_id="known-call",
        name="mcp__corsia__read_file",
        arguments={"path": "a.md"},
        db=db,
    )
    with pytest.raises(
        ToolResultBindingError,
        match="does not identify one canonical result",
    ):
        bind_tool_result_echo(
            thread_id="thread-1",
            provider_tool_call_id="unknown-call",
            assistant_step_id=step_id,
            echoed_content="anything",
            db=db,
        )
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='thread-1' AND role='tool'"
    ).fetchone()[0] == 0
    db.close()


def test_missing_result_count_borrows_db_for_production_event_context(
    isolated_db,
) -> None:
    """A db-less CC event must see the real result instead of authoring a stub."""
    from core.tool_result_delivery import persist_tool_execution_result
    from infra.database import get_db
    from subagents.cc_event_tool_settlement import _unheld_result_counts
    from subagents.child_thread import persist_tool_use

    db = get_db()
    identity = _seed_thread(db)
    step_id = persist_tool_use(
        "thread-1",
        turn_id=identity.turn_id,
        tool_id="provider-call",
        name="mcp__corsia__read_file",
        arguments={"path": "proof.md"},
        db=db,
    )
    persist_tool_execution_result(
        thread_id="thread-1",
        turn_id=identity.turn_id,
        invocation_id="invocation-call",
        assistant_step_id=step_id,
        provider_tool_call_id="provider-call",
        name="read_file",
        arguments={"path": "proof.md"},
        content="durable result",
        is_error=False,
        db=db,
    )
    db.commit()
    db.close()

    assert _unheld_result_counts(
        "thread-1", step_ids={step_id}, db=None,
    ) == {step_id: 0}


def test_echo_multi_candidate_refuses_without_mutating_rows(isolated_db) -> None:
    """Corrupt duplicate candidates remain explicit ambiguity, never guessed success."""
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.tool_result_binding import bind_tool_result_echo
    from infra.database import get_db
    from infra.queries import insert_message
    from subagents.child_thread import persist_tool_use

    db = get_db()
    identity = _seed_thread(db)
    step_id = persist_tool_use(
        "thread-1",
        turn_id=identity.turn_id,
        tool_id="provider-duplicate",
        name="mcp__corsia__read_file",
        arguments={"path": "duplicate.md"},
        db=db,
    )
    metadata = {
        "turn_id": identity.turn_id,
        "assistant_step_id": step_id,
        "tool_invocation_id": "duplicate-invocation",
        "tool_result_binding_state": "provisional",
        "name": "read_file",
        "arguments": {"path": "duplicate.md"},
    }
    for message_id in ("duplicate-result-1", "duplicate-result-2"):
        insert_message(
            "thread-1",
            "tool",
            "same result",
            metadata,
            msg_id=message_id,
            db=db,
        )

    echo = (
        "same result\n\n[CORSIA_TOOL_INVOCATION "
        'reference={"invocation_id":"duplicate-invocation"}]'
    )
    with pytest.raises(
        ToolResultBindingError,
        match="does not identify one canonical result",
    ):
        bind_tool_result_echo(
            thread_id="thread-1",
            provider_tool_call_id="provider-duplicate",
            assistant_step_id=step_id,
            echoed_content=echo,
            db=db,
        )

    states = [
        json.loads(row["metadata"])["tool_result_binding_state"]
        for row in db.execute(
            "SELECT metadata FROM messages WHERE id LIKE 'duplicate-result-%' "
            "ORDER BY id"
        ).fetchall()
    ]
    assert states == ["provisional", "provisional"]
    db.close()


@pytest.mark.asyncio
async def test_schema_invalid_call_reaching_the_bridge_gets_a_bindable_row(
    isolated_db, tmp_path,
) -> None:
    """The registry's refusal of a bad call is a canonical result its echo binds to.

    #1213: the shim used to answer a schema-invalid call itself, so no row
    existed and the echo was refused. Once the call reaches the bridge the real
    registry writes its own error as the row, and no stub is left behind.
    """
    from api import cc_attach
    from infra.database import get_db
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _LIVE, _Live
    from subagents.harness_tool_bridge import build_tool_registry

    db = get_db()
    identity = _seed_thread(db)
    live = _Live(
        session=SimpleNamespace(
            record_tool_reply=AsyncMock(), tool_context=SimpleNamespace(tokens=100),
        ),
        parent_thread_id=None,
        name="cc",
        corsia_turn=identity,
        tool_registry=build_tool_registry(str(tmp_path)),
        turn_active=True,
        last_call_usage={"input_tokens": 100},
        context_window=100_000,
        catalog_model_id="claude-code/sonnet",
    )
    _LIVE.put("thread-1", live)
    on_event = _make_on_event(live, "thread-1", lambda *_args: None, db)

    await on_event({
        "kind": "assistant_step", "text": "", "reasoning": "",
        "tool_uses": [{
            "id": "provider-invalid", "name": "mcp__corsia__read_file",
            "input": {},
        }],
    })
    projection = await cc_attach._execute_admitted_tool(
        live,
        "thread-1",
        {
            "invocation_id": "invocation-invalid",
            "provider_tool_call_id": "provider-invalid",
            "name": "read_file",
            "arguments": {},
        },
    )
    assert "Invalid parameters for tool 'read_file'" in projection

    await on_event({
        "kind": "tool_result", "tool_use_id": "provider-invalid",
        "content": projection, "is_error": False,
    })

    rows = db.execute(
        "SELECT content, metadata FROM messages "
        "WHERE thread_id='thread-1' AND role='tool'"
    ).fetchall()
    assert len(rows) == 1
    metadata = json.loads(rows[0]["metadata"])
    assert "Invalid parameters for tool 'read_file'" in rows[0]["content"]
    assert metadata["tool_call_id"] == "provider-invalid"
    assert metadata["tool_result_binding_state"] == "bound"
    assert "synthetic_reason" not in metadata
    assert live.tool_step_ids == {}
    _LIVE.pop("thread-1", None)
    db.close()
