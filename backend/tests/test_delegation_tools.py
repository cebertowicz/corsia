from __future__ import annotations

from types import SimpleNamespace

from thread.model_state import ModelSwitchOutcome
from unittest.mock import AsyncMock

import pytest

from delegation_fixtures import deleg_db  # noqa: F401


def _sync_tool_db(monkeypatch):
    """Run tool DB closures synchronously under the delegated sqlite fixture."""
    import tools.delegation as delegation_tools

    runner = AsyncMock(side_effect=lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(delegation_tools, "run_in_db", runner)
    return runner


def _cc_target(selector: str, expected: str | None = None):
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id=f"claude-code/{selector}",
        harness="claude-code",
        provider="claude-code",
        backend="cc",
        transport_model=selector,
        expected_reported_model=expected or selector,
        context_window=200000,
        available=True,
        availability_reason=None,
        display_name=selector,
        supports_reasoning=True,
    )


def test_cc_model_classification_surfaces_catalog_resolution_failure(monkeypatch):
    """A broken resolver must not silently reclassify a model as non-CC."""
    from tools import delegation

    monkeypatch.setattr(
        delegation,
        "_resolve_cc_target",
        lambda _model_id: (_ for _ in ()).throw(RuntimeError("catalog offline")),
    )

    with pytest.raises(RuntimeError, match="catalog offline"):
        delegation._is_cc_model("claude-code/sonnet")


@pytest.mark.asyncio
async def test_delegate_task_explicit_claude_code_model_spawns_cc_child(deleg_db, monkeypatch):
    from tools.delegate_task import DelegateTaskTool
    import connections.models as cm
    import subagents.cc_runtime as rt

    spawn_mock = AsyncMock(return_value="child-sonnet")
    monkeypatch.setattr(rt, "spawn", spawn_mock)
    monkeypatch.setattr(
        "tools.delegation._resolve_cc_target",
        lambda selected: _cc_target("sonnet", "claude-sonnet-4-8"),
    )
    monkeypatch.setattr(
        cm,
        "get_connection",
        lambda provider, db=None: {
            "config": {"binary": "/bin/claude", "model": "claude-fable-5"}
        },
    )
    monkeypatch.setattr(
        "tools.delegation._current_thread_context",
        lambda sid: {
            "id": sid,
            "user_id": "u-test",
            "profile": "personal",
            "project_id": "proj1",
            "title": "Parent",
        },
    )
    monkeypatch.setattr(
        "tools.delegation.resolve_spawn_working_dir",
        lambda pid: ("/ws/proj", "proj1"),
        raising=False,
    )
    monkeypatch.setattr(
        "infra.fresh_threads.resolve_spawn_working_dir",
        lambda pid: ("/ws/proj", "proj1"),
    )

    result = await DelegateTaskTool().execute(
        task="use sonnet",
        model="claude-code/sonnet",
        _thread_id="parent-1",
    )

    spawn_mock.assert_awaited_once()
    assert spawn_mock.call_args.args[:2] == ("parent-1", "use sonnet")
    target = spawn_mock.call_args.kwargs["target"]
    assert target.catalog_id == "claude-code/sonnet"
    assert target.transport_model == "sonnet"
    assert target.expected_reported_model == "claude-sonnet-4-8"
    assert "child-sonnet" in result


@pytest.mark.asyncio
async def test_delegate_task_cannot_bypass_discovered_model_cache(monkeypatch):
    from tools.delegate_task import DelegateTaskTool
    import connections.models as cm
    import subagents.cc_runtime as rt

    spawn_mock = AsyncMock(return_value="should-not-spawn")
    monkeypatch.setattr(rt, "spawn", spawn_mock)
    monkeypatch.setattr(
        cm,
        "get_connection",
        lambda provider, db=None: {"config": {"binary": "/bin/claude"}},
    )
    monkeypatch.setattr(
        "tools.delegation._resolve_cc_target",
        lambda selected: (_ for _ in ()).throw(
            ValueError(f"{selected} is not present in the canonical model catalog")
        ),
    )
    monkeypatch.setattr(
        "tools.delegation._current_thread_context",
        lambda sid: {
            "id": sid,
            "user_id": "u-test",
            "profile": "personal",
            "project_id": "proj1",
            "title": "Parent",
        },
    )
    monkeypatch.setattr(
        "infra.fresh_threads.resolve_spawn_working_dir",
        lambda pid: ("/ws/proj", "proj1"),
    )

    result = await DelegateTaskTool().execute(
        task="do not run",
        model="claude-code/invented-model",
        _thread_id="parent-1",
    )

    assert "is unknown" in result
    assert "ready selector" in result
    spawn_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_available_cache_row_supersedes_obsolete_cc_delist(deleg_db, monkeypatch):
    deleg_db.execute(
        "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,"
        "backend,source,enabled,available,context_window,metadata_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("claude-code/claude-fable-5", "claude-code", "claude-code", "claude-fable-5",
         "claude-code/claude-fable-5", "Fable", "cc", "test", 1, 1, 200000,
         '{"resolved_identity":"claude-fable-5"}', "t", "t"),
    )
    deleg_db.commit()
    from vendor_account_fixtures import signed_in_default

    signed_in_default("claude-code", db=deleg_db)  # the Default's current discovery lists fable
    from tools.delegate_task import DelegateTaskTool
    import connections.models as cm
    import subagents.cc_runtime as rt

    spawn_mock = AsyncMock(return_value="child-fable")
    monkeypatch.setattr(rt, "spawn", spawn_mock)
    monkeypatch.setattr(
        cm,
        "get_connection",
        lambda provider, db=None: {"config": {"binary": "/bin/claude"}},
    )
    monkeypatch.setattr(
        "tools.delegation._resolve_cc_target",
        lambda selected: _cc_target("claude-fable-5"),
    )
    monkeypatch.setattr(
        "tools.delegation._current_thread_context",
        lambda sid: {
            "id": sid,
            "user_id": "u-test",
            "profile": "personal",
            "project_id": "proj1",
            "title": "Parent",
        },
    )
    monkeypatch.setattr(
        "infra.fresh_threads.resolve_spawn_working_dir",
        lambda pid: ("/ws/proj", "proj1"),
    )

    result = await DelegateTaskTool().execute(
        task="use fable",
        model="claude-code/claude-fable-5",
        _thread_id="parent-1",
    )

    assert "child-fable" in result
    target = spawn_mock.await_args.kwargs["target"]
    assert target.catalog_id == "claude-code/claude-fable-5"
    assert target.transport_model == "claude-fable-5"


def test_delegate_tool_schema_names():
    from tools.delegation import (
        DelegateCreateThreadTool,
        DelegateStatusTool,
    )
    from tools.delegation_model import DelegateSetModelTool
    from tools.delegate_send import DelegateSendTool
    from tools.delegate_task import DelegateTaskTool

    assert DelegateCreateThreadTool().name == "delegate_create_thread"
    assert DelegateSendTool().name == "delegate_send"
    assert DelegateStatusTool().name == "delegate_status"
    assert DelegateSetModelTool().name == "delegate_set_model"
    assert DelegateTaskTool().name == "delegate_task"


def test_queued_delegate_receipt_uses_canonical_input_order():
    """The tool response keeps its public key while reading the receipt field."""
    from infra.send_types import InboxQueuedInput, InputReceipt
    from tools.delegation import _queued_result

    result = _queued_result(
        "target-thread",
        InboxQueuedInput(InputReceipt(
            thread_id="target-thread",
            message_id="queued-message",
            input_order=17,
            delivery_revision=3,
            created_at="t17",
        )),
    )

    assert result["queued"] == {
        "message_id": "queued-message",
        "queue_order": 17,
        "created_at": "t17",
        "delivery_revision": 3,
    }


@pytest.mark.asyncio
async def test_queued_explicit_completion_returns_canonical_queue_receipt(
    deleg_db, monkeypatch,
):
    """A queued completion exposes the same accepted FIFO receipt as progress."""
    import json

    from infra.send_types import InboxQueuedInput, InputReceipt
    from tools.delegation import _send_explicit_completion

    _sync_tool_db(monkeypatch)
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,created_at,updated_at) "
        "VALUES ('parent-1','u-test','claude-code/opus','Parent','t','t')"
    )
    deleg_db.commit()
    send = AsyncMock(return_value=InboxQueuedInput(InputReceipt(
        thread_id="parent-1",
        message_id="completion-message",
        input_order=23,
        delivery_revision=4,
        created_at="t23",
    )))
    monkeypatch.setattr("tools.delegation.send_thread_input", send)

    result = await _send_explicit_completion(
        "sender-self", "parent-1", "child-turn-1", "finished",
    )

    assert json.loads(result) == {
        "ok": True,
        "status": "queued",
        "queue_kind": "messages",
        "thread_id": "parent-1",
        "duplicate": False,
        "completion": "accepted",
        "queued": {
            "message_id": "completion-message",
            "queue_order": 23,
            "created_at": "t23",
            "delivery_revision": 4,
        },
    }


@pytest.mark.asyncio
async def test_structured_child_explicit_completion_is_refused(
    deleg_db, monkeypatch,
):
    """A structured child's return already reaches its parent; an explicit
    completion send would be a second, competing parent input, so it is
    refused rather than queued."""
    import plans.mutations as mutations
    import plans.service as service
    from tools.delegation import _send_explicit_completion

    _sync_tool_db(monkeypatch)
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,created_at,updated_at) "
        "VALUES ('parent-1','u-test','claude-code/opus','Parent','t','t')"
    )
    deleg_db.execute(
        "UPDATE threads SET spawned_by='parent-1' WHERE id='sender-self'"
    )
    deleg_db.commit()
    parent_plan = service.ensure_plan("thread", "parent-1", db=deleg_db)
    node = service.add_node(
        parent_plan.id, kind="task", title="Delegate",
        acceptance_criteria="Delegate DOD", spawn_kind="claude-code",
        db=deleg_db,
    )
    service.transition_node(node.id, "active", db=deleg_db)
    service.ensure_plan("thread", "sender-self", db=deleg_db)
    mutations.attach_spawned_thread(node.id, "sender-self", db=deleg_db)
    deleg_db.commit()

    send = AsyncMock()
    monkeypatch.setattr("tools.delegation.send_thread_input", send)

    result = await _send_explicit_completion(
        "sender-self", "parent-1", "child-turn-1", "finished",
    )

    assert result.startswith("Error:")
    assert "already delivers" in result
    assert "artifact" in result
    send.assert_not_called()


@pytest.mark.asyncio
async def test_delegate_send_tool_rejects_user_archived_target_before_write(
    deleg_db, monkeypatch
):
    _sync_tool_db(monkeypatch)
    from tools.delegate_send import DelegateSendTool

    deleg_db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,title,lifecycle_state,archived_reason,created_at,updated_at) "
        "VALUES ('archived-tool-target','u-test','claude-code/opus','Archived',"
        "'archived','user','t','t')"
    )
    deleg_db.commit()

    result = await DelegateSendTool().execute(
        "archived-tool-target",
        "must not send",
        _thread_id="sender-self",
    )

    assert result == "Error: thread is archived"
    assert (
        deleg_db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id='archived-tool-target'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_delegate_create_thread_tool_creates_chat_thread(deleg_db, monkeypatch):
    import json

    _sync_tool_db(monkeypatch)
    from tools.delegation import DelegateCreateThreadTool

    result = await DelegateCreateThreadTool().execute(
        title="External harness worker",
        model="anthropic/claude-3-5-sonnet",
        thread_kind="chat",
        _thread_id="sender-self",
    )

    body = json.loads(result)
    assert body["ok"] is True
    assert body["thread_kind"] == "chat"
    row = deleg_db.execute(
        "SELECT title, model, project_id, spawned_by, parent_thread_id "
        "FROM threads WHERE id = ?",
        (body["thread_id"],),
    ).fetchone()
    assert dict(row) == {
        "title": "External harness worker",
        "model": "anthropic/claude-3-5-sonnet",
        "project_id": None,
        "spawned_by": None,
        "parent_thread_id": None,
    }


@pytest.mark.asyncio
async def test_delegate_send_tool_blocks_cross_scope_with_repair(
    deleg_db,
    monkeypatch,
):
    import json

    _sync_tool_db(monkeypatch)
    from tools.delegate_send import DelegateSendTool

    deleg_db.execute(
        "UPDATE threads SET project_id = 'proj-a' WHERE id = 'sender-self'"
    )
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,project_id,created_at,updated_at) "
        "VALUES ('target-other-scope', 'u-test', 'anthropic/claude-3-5-sonnet', "
        "'Other Scope', 'proj-b','t','t')"
    )
    deleg_db.commit()

    send_mock = AsyncMock()
    marker_mock = AsyncMock()
    monkeypatch.setattr("tools.delegation.send_thread_input", send_mock, raising=False)
    monkeypatch.setattr(
        "infra.delegation_markers.emit_send_ref_marker",
        marker_mock,
    )

    result = await DelegateSendTool().execute(
        "target-other-scope",
        "do the thing",
        _thread_id="sender-self",
    )

    assert result.startswith("Error: ")
    detail = json.loads(result.removeprefix("Error: "))
    assert detail["code"] == "cross_scope_send_blocked"
    assert detail["source_scope"] == "project:proj-a"
    assert detail["target_scope"] == "project:proj-b"
    assert detail["repair_action"] == "configure_target_scope_authority"
    send_mock.assert_not_called()
    marker_mock.assert_not_called()


@pytest.mark.asyncio
async def test_delegate_send_tool_reaches_workspace_conductor(deleg_db, monkeypatch):
    import json
    from types import SimpleNamespace

    _sync_tool_db(monkeypatch)
    from tools.delegate_send import DelegateSendTool

    deleg_db.execute(
        "UPDATE threads SET project_id='proj-a', agent_enabled=1 "
        "WHERE id='sender-self'"
    )
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,agent_enabled,created_at,updated_at) "
        "VALUES ('workspace-conductor','u-test','claude-code/sonnet',"
        "'Workspace conductor',1,'t','t')"
    )
    deleg_db.execute(
        "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
        (
            "corsia_agent_thread_id",
            json.dumps({"session_id": "workspace-conductor"}),
            "t",
        ),
    )
    deleg_db.commit()

    send_mock = AsyncMock(
        return_value=SimpleNamespace(driver="cc_runtime.feed_main", duplicate=False)
    )
    monkeypatch.setattr("tools.delegation.send_thread_input", send_mock)

    result = await DelegateSendTool().execute(
        "workspace-conductor",
        "Please update workspace policy",
        _thread_id="sender-self",
        _host_access_turn_identity=object(),
    )

    assert json.loads(result)["ok"] is True
    send_mock.assert_awaited_once()
    assert send_mock.call_args.kwargs["metadata"]["from_thread"] == "sender-self"

    reply = await DelegateSendTool().execute(
        "sender-self", "I can help", _thread_id="workspace-conductor",
        _host_access_turn_identity=object(), target_scope_id="proj-a",
    )
    assert json.loads(reply)["ok"] is True
    assert send_mock.await_count == 2
    assert send_mock.call_args.kwargs["metadata"]["from_thread"] == "workspace-conductor"

    # The designation is checked on every send, so a former conductor loses
    # the reverse privilege without waiting for a restart or stale roster.
    deleg_db.execute(
        "UPDATE settings SET value=? WHERE key='corsia_agent_thread_id'",
        (json.dumps({"session_id": None}),),
    )
    deleg_db.commit()
    stale = await DelegateSendTool().execute(
        "sender-self", "stale", _thread_id="workspace-conductor",
        _host_access_turn_identity=object(),
    )
    assert json.loads(stale.removeprefix("Error: "))["code"] == "cross_scope_send_blocked"
    assert send_mock.await_count == 2


@pytest.mark.asyncio
async def test_delegate_send_tool_blocks_matching_cross_scope_target_scope(
    deleg_db,
    monkeypatch,
):
    import json

    _sync_tool_db(monkeypatch)
    from tools.delegate_send import DelegateSendTool

    deleg_db.execute(
        "UPDATE threads SET project_id = 'proj-a' WHERE id = 'sender-self'"
    )
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,project_id,created_at,updated_at) "
        "VALUES ('target-other-scope', 'u-test', 'anthropic/claude-3-5-sonnet', "
        "'Other Scope', 'proj-b','t','t')"
    )
    deleg_db.commit()

    send_mock = AsyncMock()
    marker_mock = AsyncMock()
    monkeypatch.setattr("tools.delegation.send_thread_input", send_mock, raising=False)
    monkeypatch.setattr(
        "infra.delegation_markers.emit_send_ref_marker",
        marker_mock,
    )

    result = await DelegateSendTool().execute(
        "target-other-scope",
        "do the thing",
        target_scope_id="proj-b",
        _thread_id="sender-self",
    )

    assert result.startswith("Error: ")
    detail = json.loads(result.removeprefix("Error: "))
    assert detail["code"] == "cross_scope_send_blocked"
    assert detail["source_scope"] == "project:proj-a"
    assert detail["target_scope"] == "project:proj-b"
    send_mock.assert_not_called()
    marker_mock.assert_not_called()


@pytest.mark.asyncio
async def test_delegate_send_tool_rejects_mismatched_target_scope(
    deleg_db,
    monkeypatch,
):
    import json

    _sync_tool_db(monkeypatch)
    from tools.delegate_send import DelegateSendTool

    deleg_db.execute(
        "UPDATE threads SET project_id = 'proj-a' WHERE id = 'sender-self'"
    )
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,project_id,created_at,updated_at) "
        "VALUES ('target-other-scope', 'u-test', 'anthropic/claude-3-5-sonnet', "
        "'Other Scope', 'proj-b','t','t')"
    )
    deleg_db.commit()

    send_mock = AsyncMock()
    marker_mock = AsyncMock()
    monkeypatch.setattr("tools.delegation.send_thread_input", send_mock, raising=False)
    monkeypatch.setattr(
        "infra.delegation_markers.emit_send_ref_marker",
        marker_mock,
    )

    result = await DelegateSendTool().execute(
        "target-other-scope",
        "do the thing",
        target_scope_id="proj-a",
        _thread_id="sender-self",
    )

    assert result.startswith("Error: ")
    detail = json.loads(result.removeprefix("Error: "))
    assert detail["code"] == "target_scope_mismatch"
    send_mock.assert_not_called()
    marker_mock.assert_not_called()


@pytest.mark.asyncio
async def test_delegate_status_tool_reads_runtime_truth_and_recent_messages(
    deleg_db,
    monkeypatch,
):
    import json

    _sync_tool_db(monkeypatch)
    from tools.delegation import DelegateStatusTool

    deleg_db.execute(
        "INSERT INTO projects "
        "(id,name,conductor_thread_id,created_at,updated_at) "
        "VALUES ('proj-status','Status Project','status-target','t','t')"
    )
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,project_id,created_at,updated_at) "
        "VALUES ('status-target', 'u-test', 'anthropic/claude-3-5-sonnet', "
        "'Status Target', 'proj-status','t','t')"
    )
    deleg_db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('turn-status','status-target',0,'anthropic/claude-3-5-sonnet',"
        "'running','2026-07-04T00:00:00','boot-test')"
    )
    deleg_db.execute(
        "INSERT INTO messages (id, thread_id, role, content, created_at) "
        "VALUES ('msg-status', 'status-target', 'assistant', 'ready', "
        "'2026-07-04T00:01:00')"
    )
    deleg_db.commit()

    result = await DelegateStatusTool().execute(
        "status-target",
        messages=1,
        _thread_id="sender-self",
    )

    body = json.loads(result)
    assert body["thread_id"] == "status-target"
    assert body["driver"] == "engine"
    assert body["turn_active"] is True
    assert body["recent_messages"][-1]["content"] == "ready"
    assert body["conductor_scopes"] == [
        {
            "scope": "proj-status",
            "scope_type": "project",
            "project_name": "Status Project",
        }
    ]


@pytest.mark.asyncio
async def test_delegate_status_reads_the_current_model_of_a_queued_handoff(deleg_db, monkeypatch):
    """The onboarding prompts read "waiting for a model" only from ``model == ""`` here."""
    import json

    from delegation_fixtures import insert_thread

    _sync_tool_db(monkeypatch)
    from tools.delegation import DelegateStatusTool

    insert_thread(deleg_db, "modelless", model="", title="New project conductor")

    async def status():
        return await DelegateStatusTool().execute("modelless", messages=0, _thread_id="sender-self")

    assert json.loads(await status())["model"] == ""
    deleg_db.execute("UPDATE threads SET model='claude-code/opus' WHERE id='modelless'")
    deleg_db.commit()
    assert json.loads(await status())["model"] == "claude-code/opus"


@pytest.mark.asyncio
async def test_delegate_status_of_another_users_thread_is_an_error(deleg_db, monkeypatch):
    from delegation_fixtures import insert_thread

    _sync_tool_db(monkeypatch)
    from tools.delegation import DelegateStatusTool

    insert_thread(deleg_db, "foreign", user_id="u-other", model="", title="Other")

    result = await DelegateStatusTool().execute("foreign", messages=0, _thread_id="sender-self")
    assert result == "Error: Thread not found: foreign"


@pytest.mark.asyncio
async def test_delegate_status_reports_current_switch_and_checkpoint_matches(
    deleg_db,
    monkeypatch,
):
    import json

    from tools.delegation import DelegateStatusTool

    _sync_tool_db(monkeypatch)
    deleg_db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,title,created_at,updated_at,next_message_seq,"
        "pending_model,pending_execution_epoch,switch_after_input_order,"
        "switch_operation_id,switch_status,switch_requested_at,"
        "switch_checkpoint_choice) VALUES "
        "('cutover-target','u-test','codex/gpt-5-codex','Cutover Target',"
        "'t','t',2,'anthropic/claude-3-5-sonnet',1,1,"
        "'operation-status','preparing','t','current')"
    )
    deleg_db.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch) "
        "VALUES ('cutover-turn','cutover-target',0,'codex/gpt-5-codex',"
        "'completed','t','t','boot-test')"
    )
    deleg_db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) "
        "VALUES ('cutover-message','cutover-target','user','hello',?, 't')",
        (json.dumps({"turn_id": "cutover-turn"}),),
    )
    deleg_db.execute(
        "UPDATE messages SET seq=1 WHERE id='cutover-message'"
    )
    deleg_db.commit()
    operation_id = "operation-status"
    checkpoint_id = "checkpoint-status"
    deleg_db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,previous_generation_id,through_seq,"
        "through_message_id,trigger,mode,status,source_harness,source_model,"
        "writer_harness,writer_model,continuity_base_revision,"
        "continuity_base_snapshot_json,started_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            checkpoint_id,
            "cutover-target",
            0,
            None,
            1,
            "cutover-message",
            "model_switch",
            "warm",
            "running",
            "codex",
            "codex/gpt-5-codex",
            "codex",
            "codex/gpt-5-codex",
            0,
            "{}",
            "t",
        ),
    )
    deleg_db.commit()

    body = json.loads(
        await DelegateStatusTool().execute(
            "cutover-target",
            messages=0,
            operation_id=operation_id,
            checkpoint_id=checkpoint_id,
            _thread_id="sender-self",
        )
    )

    assert body["model_switch"] == {
        "operation_id": operation_id,
        "pending_model": "anthropic/claude-3-5-sonnet",
        "status": "preparing",
        "active": True,
        "checkpoint_model": "current",
        "error": None,
        "requested_at": "t",
    }
    assert body["checkpoint"]["id"] == checkpoint_id
    assert body["checkpoint"]["status"] == "running"
    assert body["requested_operation_id"] == operation_id
    assert body["requested_operation_matches"] is True
    assert body["requested_checkpoint_id"] == checkpoint_id
    assert body["requested_checkpoint_matches"] is True

    mismatch = json.loads(
        await DelegateStatusTool().execute(
            "cutover-target",
            messages=0,
            operation_id="different-operation",
            _thread_id="sender-self",
        )
    )
    assert mismatch["requested_operation_matches"] is False


@pytest.mark.asyncio
async def test_delegate_set_model_tool_updates_model_and_audit_breadcrumb(
    deleg_db,
    monkeypatch,
):
    import json

    _sync_tool_db(monkeypatch)
    from tools.delegation_model import DelegateSetModelTool

    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,created_at,updated_at) "
        "VALUES ('model-target','u-test','codex/gpt-5-codex','Model Target','t','t')"
    )
    deleg_db.commit()
    effects = AsyncMock()
    monkeypatch.setattr("thread.model.post_model_switch_effects", effects)

    result = await DelegateSetModelTool().execute(
        "model-target",
        "anthropic/claude-3-5-sonnet",
        reason="repair",
        _thread_id="sender-self",
    )

    body = json.loads(result)
    assert body["ok"] is True
    assert body["changed"] is True
    assert body["old_model"] == "codex/gpt-5-codex"
    row = deleg_db.execute(
        "SELECT model FROM threads WHERE id = 'model-target'"
    ).fetchone()
    assert row["model"] == "anthropic/claude-3-5-sonnet"
    audit = deleg_db.execute(
        "SELECT content, metadata FROM messages WHERE thread_id = 'model-target'"
    ).fetchone()
    # Plain words for a person; the ids and the reason code live in metadata,
    # which is where the machine reads them (owner decision, 2026-08-10).
    assert audit["content"] == "Switched to Claude 3.5 Sonnet"
    metadata = json.loads(audit["metadata"])
    assert metadata["new_model"] == "anthropic/claude-3-5-sonnet"
    assert metadata["reason"] == "repair"
    effects.assert_awaited_once()


@pytest.mark.asyncio
async def test_delegate_set_model_tool_uses_canonical_prepared_switch(
    deleg_db,
    monkeypatch,
):
    import json

    _sync_tool_db(monkeypatch)
    from tools.delegation_model import DelegateSetModelTool

    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,created_at,updated_at) "
        "VALUES ('prepared-target','u-test','codex/gpt-5-codex','Target','t','t')"
    )
    deleg_db.commit()
    outcome = ModelSwitchOutcome(
        changed=True,
        old_model="codex/gpt-5-codex",
        new_model="anthropic/claude-3-5-sonnet",
        preparation=SimpleNamespace(projection_mode="checkpoint_tail"),
        breadcrumb=None,
        committed_at="2026-07-17T08:00:00.000000",
        post_effects_error="retirement failed",
    )
    switch = AsyncMock(return_value=outcome)
    monkeypatch.setattr(
        "thread.model.switch_model",
        switch,
    )

    result = await DelegateSetModelTool().execute(
        "prepared-target",
        "anthropic/claude-3-5-sonnet",
        reason="repair",
        _thread_id="sender-self",
    )

    body = json.loads(result)
    assert body["ok"] is True
    assert body["changed"] is True
    assert "checkpoint continuity" in body["continuity"]
    assert "switch committed" in body["warning"]
    assert "retirement failed" in body["warning"]
    switch.assert_awaited_once_with(
        "prepared-target",
        "anthropic/claude-3-5-sonnet",
        user_id="u-test",
        actor_kind="agent",
        actor_id="sender-self",
        reason="repair",
        checkpoint_override=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "choice"),
    [(None, "current"), ("target", "target")],
)
async def test_delegate_set_model_returns_durable_pending_switch_receipt(
    deleg_db,
    monkeypatch,
    override: str | None,
    choice: str,
):
    import json

    from infra.model_switch_types import SwitchRequest
    from tools.delegation_model import DelegateSetModelTool

    _sync_tool_db(monkeypatch)
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,created_at,updated_at) "
        "VALUES ('pending-target','u-test','codex/gpt-5-codex','Target','t','t')"
    )
    deleg_db.commit()
    switch = AsyncMock(
        return_value=SwitchRequest(
            operation_id="operation-1",
            thread_id="pending-target",
            source_model="codex/gpt-5-codex",
            destination_model="anthropic/claude-3-5-sonnet",
            source_execution_epoch=0,
            destination_execution_epoch=1,
            switch_after_input_order=0,
            actor_kind="agent",
            actor_id="sender-self",
            reason="repair",
            checkpoint_choice=choice,
            requested_at="t",
            observed_input_order=0,
            waiting_for_source=True,
        )
    )
    monkeypatch.setattr("thread.model.switch_model", switch)

    result = await DelegateSetModelTool().execute(
        "pending-target",
        "anthropic/claude-3-5-sonnet",
        reason="repair",
        checkpoint_model=override,
        _thread_id="sender-self",
    )

    body = json.loads(result)
    assert body == {
        "accepted": True,
        "checkpoint_model": choice,
        "instruction": (
            "Call delegate_status with operation_id=operation-1 to observe "
            "completion or failure."
        ),
        "operation_id": "operation-1",
        "thread_id": "pending-target",
        "status": "waiting_for_turn",
        "switched": False,
        "target_model": "anthropic/claude-3-5-sonnet",
        "pending_model": "anthropic/claude-3-5-sonnet",
        "checkpoint_required": False,
    }
    switch.assert_awaited_once()
    switch.assert_awaited_once_with(
        "pending-target",
        "anthropic/claude-3-5-sonnet",
        user_id="u-test",
        actor_kind="agent",
        actor_id="sender-self",
        reason="repair",
        checkpoint_override=override,
    )


@pytest.mark.asyncio
async def test_delegate_set_model_same_destination_returns_durable_noop(
    deleg_db,
    monkeypatch,
):
    import json

    from tools.delegation_model import DelegateSetModelTool

    _sync_tool_db(monkeypatch)
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,created_at,updated_at) "
        "VALUES ('raced-target','u-test','anthropic/claude-3-5-sonnet',"
        "'Target','t','raced')"
    )
    deleg_db.commit()

    result = await DelegateSetModelTool().execute(
        "raced-target",
        "anthropic/claude-3-5-sonnet",
        reason="repair",
        _thread_id="sender-self",
    )

    assert json.loads(result) == {
        "ok": True,
        "thread_id": "raced-target",
        "old_model": "anthropic/claude-3-5-sonnet",
        "new_model": "anthropic/claude-3-5-sonnet",
        "changed": False,
        "model_updated_at": "raced",
        "continuity": (
            "unchanged: the selected model did not change, so no context "
            "reconstruction was required."
        ),
    }
    row = deleg_db.execute(
        "SELECT switch_operation_id FROM threads WHERE id='raced-target'"
    ).fetchone()
    assert row["switch_operation_id"] is None


@pytest.mark.asyncio
async def test_delegate_set_model_accepts_only_target_checkpoint_override(
    deleg_db,
    monkeypatch,
):
    from tools.delegation_model import DelegateSetModelTool

    _sync_tool_db(monkeypatch)

    result = await DelegateSetModelTool().execute(
        "anything",
        "anthropic/claude-3-5-sonnet",
        checkpoint_model="current",
        _thread_id="sender-self",
    )

    assert result == (
        "Error: checkpoint_model may be omitted (current) or set to target."
    )


def test_delegation_tool_schemas_require_model():
    from tools.delegate_task import DelegateTaskTool
    from tools.delegation import DelegateCreateThreadTool

    for tool in (DelegateTaskTool(), DelegateCreateThreadTool()):
        schema = tool.parameters
        assert "model" in schema["required"]
        assert schema["properties"]["model"]["type"] == "string"
        assert schema["properties"]["model"]["pattern"] == r"\S"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [None, "", "  "])
async def test_delegation_tools_reject_missing_model_before_create(deleg_db, monkeypatch, model):
    from infra.settings import set_setting
    from tools.delegate_task import DelegateTaskTool
    from tools.delegation import DelegateCreateThreadTool

    _sync_tool_db(monkeypatch)
    set_setting("agents", {"defaults": {"model": "anthropic/claude-3-5-sonnet"}})
    spawn = AsyncMock()
    monkeypatch.setattr("tools.delegate_task.create_visible_fresh_thread", spawn)
    monkeypatch.setattr("tools.delegation.create_visible_fresh_thread", spawn)
    before = deleg_db.execute("SELECT COUNT(*) FROM threads").fetchone()[0]

    created = await DelegateCreateThreadTool().execute(
        title="T", model=model, thread_kind="chat", _thread_id="sender-self",
    )
    task = await DelegateTaskTool().execute(task="do it", model=model, _thread_id="sender-self")

    assert created.startswith("Error: No model for delegated thread")
    assert task.startswith("Error: No model for delegated thread")
    spawn.assert_not_awaited()
    assert deleg_db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == before
