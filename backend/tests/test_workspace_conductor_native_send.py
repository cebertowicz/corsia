"""Native delegate_send between project agents and the workspace conductor.

Each send is a provider-declared Claude Code tool call executed through the
real bridge: the declared-call claim, ToolRegistry identity binding from the
sender's running Turn, DelegateSendTool, and unmocked send_thread_input
admission and dispatch to an idle target. Only the harness process launch
(cc_runtime.feed_main) and post-tool provider reply settlement are stubbed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.project_ref_helpers import (
    project_ref,
    select_workspace,
    write_project_context,
)

_CONDUCTOR = "workspace-conductor"
_AGENT_A = "agent-a"


def _insert_agent(db, thread_id: str, *, user_id: str = "u-test",
                  project_id: str | None = None, agent_enabled: int = 1) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,title,project_id,agent_enabled,"
        "created_at,updated_at) VALUES (?,?,'claude-code/sonnet',?,?,?,'t','t')",
        (thread_id, user_id, thread_id, project_id, agent_enabled),
    )
    db.commit()


def _start_live(db, thread_id: str):
    """Give an inserted agent a running Turn and its bound CC live."""
    from core.turn_core import BOOT_EPOCH, persist_turn_start
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input
    from subagents.cc_runtime_state import _LIVE, _Live
    from tools.delegate_send import DelegateSendTool
    from tools.registry import ToolRegistry

    turn_id = f"{thread_id}:turn"
    admission = admit_input(
        thread_id, role="user", content="work", intent="apply_now",
        message_id=f"{turn_id}:input", turn_id=turn_id, boot_epoch=BOOT_EPOCH,
        admitted_at="t", db=db,
    )
    begin_batch_write(admission.batch, attempt_id=f"{turn_id}:write", started_at="t", db=db)
    settle_batch_write(f"{turn_id}:write", outcome="accepted", db=db)
    identity = persist_turn_start(turn_id, thread_id, "claude-code/sonnet", "work", db=db)
    db.commit()
    registry = ToolRegistry()
    registry.register(DelegateSendTool())
    live = _Live(
        session=SimpleNamespace(record_tool_reply=AsyncMock(),
                                tool_context=SimpleNamespace(tokens=100)),
        parent_thread_id=None, name=thread_id, corsia_turn=identity,
        tool_registry=registry, turn_active=True,
        last_call_usage={"input_tokens": 100}, context_window=100_000,
        catalog_model_id="claude-code/sonnet",
    )
    _LIVE.put(thread_id, live)
    return live


async def _native_send(db, live, thread_id: str, call_id: str, arguments: dict) -> str:
    """Declare one provider delegate_send call, then execute it through the bridge."""
    from api import cc_attach
    from subagents.cc_runtime_events import _make_on_event

    on_event = _make_on_event(live, thread_id, lambda *_args: None, db)
    await on_event({
        "kind": "assistant_step", "text": "", "reasoning": "",
        "tool_uses": [{"id": call_id, "name": "mcp__corsia__delegate_send",
                       "input": arguments}],
    })
    return await cc_attach._execute_admitted_tool(live, thread_id, {
        "invocation_id": f"{call_id}:invocation", "provider_tool_call_id": call_id,
        "name": "delegate_send", "arguments": arguments,
    })


def _ok(projection: str) -> bool:
    """Read the tool's JSON ahead of the bridge's invocation reference."""
    return json.JSONDecoder().raw_decode(projection)[0]["ok"] is True


def _inbound(db, thread_id: str) -> list[tuple[str, str]]:
    rows = db.execute(
        "SELECT content,metadata FROM messages WHERE thread_id=? AND role='user'",
        (thread_id,),
    ).fetchall()
    return [(json.loads(row["metadata"] or "{}").get("from_thread"), row["content"])
            for row in rows]


@pytest.fixture
def launched(monkeypatch) -> list[tuple[str, str]]:
    """Accept the target's harness turn at the process-launch boundary."""
    from subagents import cc_runtime

    calls: list[tuple[str, str]] = []

    async def feed_main(thread_id, content, **kwargs):
        await kwargs["persist_input_once"]()
        kwargs["mark_accepted"]()
        calls.append((thread_id, content))
        return kwargs["preadmitted_turn"]

    monkeypatch.setattr(cc_runtime, "feed_main", feed_main)
    return calls


@pytest.fixture
def db(isolated_db, tmp_path, monkeypatch):
    from infra.database import get_db
    from subagents.cc_runtime_state import _LIVE

    # Post-tool provider reply and capacity settlement has its own suites.
    monkeypatch.setattr("subagents.harness_tool_bridge.settle_cc_tool_result", AsyncMock())
    conn = get_db()
    select_workspace(monkeypatch, tmp_path)
    for project_id in ("proj-a", "proj-b"):
        write_project_context(tmp_path, project_id)
        conn.execute(
            "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES (?,?,?,'t','t')",
            (project_id, project_id, project_ref(project_id)),
        )
    conn.commit()
    yield conn
    # Every thread this module can start a live harness for: _LIVE is
    # process-global, so a handle left here outlives the database it was
    # built against and the next test dispatches into a dead session.
    for thread_id in (_CONDUCTOR, _AGENT_A, _PROJ_CONDUCTOR, _FOREIGN_CONDUCTOR,
                      "proj-a-conductor-2", "agent-b", "plain-chat"):
        _LIVE.pop(thread_id, None)
    conn.close()


@pytest.mark.asyncio
async def test_project_agent_native_send_reaches_designated_conductor(db, launched):
    from infra.conductor import set_conductor

    _insert_agent(db, _AGENT_A, project_id="proj-a")
    _insert_agent(db, _CONDUCTOR)
    _insert_agent(db, "agent-b", project_id="proj-b")
    assert set_conductor("workspace", _CONDUCTOR) is True
    agent = _start_live(db, _AGENT_A)

    # A forged sender in provider arguments is stripped; the Turn binds identity.
    sent = await _native_send(db, agent, _AGENT_A, "call-up", {
        "thread_id": _CONDUCTOR, "message": "Please update workspace policy",
        "_thread_id": "agent-b",
    })
    assert _ok(sent), sent
    assert _inbound(db, _CONDUCTOR) == [(_AGENT_A, "Please update workspace policy")]

    unrelated = await _native_send(db, agent, _AGENT_A, "call-sideways", {
        "thread_id": "agent-b", "message": "cross project",
    })
    assert "cross_scope_send_blocked" in unrelated
    assert _inbound(db, "agent-b") == []
    [(target, rendered)] = launched
    assert target == _CONDUCTOR
    assert '[AGENT-SEND — from "agent-a" (thread agent-a), scope proj-a]' in rendered


@pytest.mark.asyncio
async def test_designated_conductor_native_reply_reaches_project_agent(db, launched):
    from infra.conductor import set_conductor

    _insert_agent(db, _AGENT_A, project_id="proj-a")
    _insert_agent(db, _CONDUCTOR)
    assert set_conductor("workspace", _CONDUCTOR) is True
    conductor = _start_live(db, _CONDUCTOR)

    reply = await _native_send(db, conductor, _CONDUCTOR, "call-down", {
        "thread_id": _AGENT_A, "message": "Policy updated",
    })
    assert _ok(reply), reply
    assert _inbound(db, _AGENT_A) == [(_CONDUCTOR, "Policy updated")]

    # Designation is read on every send; a former conductor loses the privilege.
    assert set_conductor("workspace", None) is True
    stale = await _native_send(db, conductor, _CONDUCTOR, "call-stale", {
        "thread_id": _AGENT_A, "message": "former conductor",
    })
    assert "cross_scope_send_blocked" in stale
    assert _inbound(db, _AGENT_A) == [(_CONDUCTOR, "Policy updated")]
    [(target, rendered)] = launched
    assert target == _AGENT_A
    header = '[AGENT-SEND — from "workspace-conductor" (thread workspace-conductor), scope workspace]'
    assert header in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sender", "target"), [(_AGENT_A, _CONDUCTOR), (_CONDUCTOR, _AGENT_A)],
)
async def test_designated_conductor_needs_no_agent_registration(
    db, launched, sender, target,
):
    """Production shape (#2634): the conductor is designated, never promoted."""
    from infra.conductor import set_conductor

    _insert_agent(db, _AGENT_A, project_id="proj-a")
    _insert_agent(db, _CONDUCTOR, agent_enabled=0)
    assert set_conductor("workspace", _CONDUCTOR) is True
    live = _start_live(db, sender)

    sent = await _native_send(db, live, sender, "call", {
        "thread_id": target, "message": "Need a workspace decision",
    })
    assert _ok(sent), sent
    assert _inbound(db, target) == [(sender, "Need a workspace decision")]
    assert [thread_id for thread_id, _ in launched] == [target]


@pytest.mark.asyncio
async def test_unregistered_non_conductor_endpoint_is_refused_with_reason(db, launched):
    from infra.conductor import set_conductor

    _insert_agent(db, _AGENT_A, project_id="proj-a")
    _insert_agent(db, _CONDUCTOR)
    _insert_agent(db, "plain-chat", project_id="proj-b", agent_enabled=0)
    assert set_conductor("workspace", _CONDUCTOR) is True
    conductor = _start_live(db, _CONDUCTOR)

    refused = await _native_send(db, conductor, _CONDUCTOR, "call-chat", {
        "thread_id": "plain-chat", "message": "hello",
    })
    assert (
        "cross-scope send requires active agent endpoints; "
        "target plain-chat: thread is not a registered agent"
    ) in refused, refused
    assert _inbound(db, "plain-chat") == []
    assert launched == []


@pytest.mark.asyncio
async def test_native_send_denies_foreign_user_conductor_pair(db, launched):
    from infra.conductor import set_conductor

    _insert_agent(db, _AGENT_A, project_id="proj-a")
    _insert_agent(db, _CONDUCTOR, user_id="u-other")
    assert set_conductor("workspace", _CONDUCTOR) is True
    agent = _start_live(db, _AGENT_A)
    conductor = _start_live(db, _CONDUCTOR)

    up = await _native_send(db, agent, _AGENT_A, "call-up", {
        "thread_id": _CONDUCTOR, "message": "cross-user",
    })
    down = await _native_send(db, conductor, _CONDUCTOR, "call-down", {
        "thread_id": _AGENT_A, "message": "cross-user",
    })
    assert up.startswith(f"Error: Thread not found: {_CONDUCTOR}"), up
    assert down.startswith(f"Error: Thread not found: {_AGENT_A}"), down
    assert _inbound(db, _CONDUCTOR) == [(None, "work")]
    assert _inbound(db, _AGENT_A) == [(None, "work")]
    assert launched == []


_PROJ_CONDUCTOR = "proj-a-conductor"
_FOREIGN_CONDUCTOR = "foreign-conductor"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sender", "target"),
    [(_CONDUCTOR, _PROJ_CONDUCTOR), (_PROJ_CONDUCTOR, _CONDUCTOR)],
)
async def test_workspace_and_project_conductors_reach_each_other(db, launched, sender, target):
    """The handoff and its report-back are the two admitted directions (#3287 BLK1).

    Both conductors are designated, never promoted: neither is
    ``agent_enabled``, which is what made both directions refuse.
    """
    from infra.conductor import set_conductor

    _insert_agent(db, _CONDUCTOR, agent_enabled=0)
    _insert_agent(db, _PROJ_CONDUCTOR, project_id="proj-a", agent_enabled=0)
    assert set_conductor("workspace", _CONDUCTOR) is True
    assert set_conductor("proj-a", _PROJ_CONDUCTOR) is True
    live = _start_live(db, sender)

    sent = await _native_send(db, live, sender, "call-handoff", {
        "thread_id": target, "message": "Import this project",
    })
    assert _ok(sent), sent
    assert _inbound(db, target) == [(sender, "Import this project")]
    assert [thread_id for thread_id, _ in launched] == [target]


@pytest.mark.asyncio
async def test_conductor_designated_for_another_project_stays_unregistered(db, launched):
    """S1: a designation is only an endpoint inside the project that made it.

    ``PUT /api/conductor`` checks the thread belongs to the account, not to the
    scope, so proj-a can be made to designate a thread that lives in proj-b.
    That thread must stay unreachable in proj-b, where it is an ordinary chat.
    """
    from infra.conductor import set_conductor

    _insert_agent(db, _CONDUCTOR, agent_enabled=0)
    _insert_agent(db, _FOREIGN_CONDUCTOR, project_id="proj-b", agent_enabled=0)
    assert set_conductor("workspace", _CONDUCTOR) is True
    assert set_conductor("proj-a", _FOREIGN_CONDUCTOR) is True
    conductor = _start_live(db, _CONDUCTOR)

    refused = await _native_send(db, conductor, _CONDUCTOR, "call-mismatch", {
        "thread_id": _FOREIGN_CONDUCTOR, "message": "mismatched designation",
    })
    assert (
        "cross-scope send requires active agent endpoints; "
        f"target {_FOREIGN_CONDUCTOR}: thread is not a registered agent"
    ) in refused, refused
    assert _inbound(db, _FOREIGN_CONDUCTOR) == []
    assert launched == []


@pytest.mark.asyncio
async def test_former_project_conductor_refused_after_redesignation(db, launched):
    """The designation is read live: the replaced conductor loses the privilege."""
    from infra.conductor import set_conductor

    _insert_agent(db, _CONDUCTOR, agent_enabled=0)
    _insert_agent(db, _PROJ_CONDUCTOR, project_id="proj-a", agent_enabled=0)
    _insert_agent(db, "proj-a-conductor-2", project_id="proj-a", agent_enabled=0)
    assert set_conductor("workspace", _CONDUCTOR) is True
    assert set_conductor("proj-a", _PROJ_CONDUCTOR) is True
    conductor = _start_live(db, _CONDUCTOR)

    first = await _native_send(db, conductor, _CONDUCTOR, "call-first", {
        "thread_id": _PROJ_CONDUCTOR, "message": "before re-designation",
    })
    assert _ok(first), first

    assert set_conductor("proj-a", "proj-a-conductor-2") is True
    stale = await _native_send(db, conductor, _CONDUCTOR, "call-stale", {
        "thread_id": _PROJ_CONDUCTOR, "message": "after re-designation",
    })
    assert (
        "cross-scope send requires active agent endpoints; "
        f"target {_PROJ_CONDUCTOR}: thread is not a registered agent"
    ) in stale, stale
    assert _inbound(db, _PROJ_CONDUCTOR) == [
        (_CONDUCTOR, "before re-designation"),
    ]
    assert [thread_id for thread_id, _ in launched] == [_PROJ_CONDUCTOR]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["plain-chat", "agent-a"])
async def test_project_to_project_send_still_refused_at_scope(db, launched, target):
    """Only a pair including the workspace conductor crosses scope (S3)."""
    from infra.conductor import set_conductor

    _insert_agent(db, _AGENT_A, project_id="proj-b")
    _insert_agent(db, _CONDUCTOR, agent_enabled=0)
    _insert_agent(db, _PROJ_CONDUCTOR, project_id="proj-a", agent_enabled=0)
    _insert_agent(db, "plain-chat", project_id="proj-b", agent_enabled=0)
    assert set_conductor("workspace", _CONDUCTOR) is True
    assert set_conductor("proj-a", _PROJ_CONDUCTOR) is True
    conductor = _start_live(db, _CONDUCTOR)
    del conductor  # the sender is the project conductor, not the workspace one

    sender = _start_live(db, _PROJ_CONDUCTOR)
    refused = await _native_send(db, sender, _PROJ_CONDUCTOR, "call-sideways", {
        "thread_id": target, "message": "project to project",
    })
    assert "cross_scope_send_blocked" in refused, refused
    assert _inbound(db, target) == []
    assert launched == []


def test_corsia_agent_contact_output_ignores_project_conductors(db):
    """S2: listing the workspace contact is unchanged when a project designates one."""
    from core.agent_roster_queries import corsia_agent_contact
    from infra.conductor import set_conductor

    _insert_agent(db, _CONDUCTOR, agent_enabled=0)
    _insert_agent(db, _PROJ_CONDUCTOR, project_id="proj-a", agent_enabled=0)
    assert set_conductor("workspace", _CONDUCTOR) is True

    before = corsia_agent_contact("u-test", db)
    assert set_conductor("proj-a", _PROJ_CONDUCTOR) is True

    assert corsia_agent_contact("u-test", db) == before
    contact, gap = before
    assert gap is None
    assert contact is not None and contact["id"] == _CONDUCTOR
