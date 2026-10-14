"""Exercise managed notice failures through ToolRegistry and durable result delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path

import pytest

from infra.platform_paths import WorkspaceSelection
from tests.registry_helpers import make_session

_NOTICE_FAILURE = (
    "File saved. The context-change notice could not be recorded; other agents "
    "may still use earlier instructions."
)


@pytest.fixture
def file_scope(isolated_db, tmp_path, monkeypatch):
    """Create one governed project and its exact active tool thread."""
    from infra import config
    from infra.database import get_db
    from registry import service
    from tools import filesystem_edit, filesystem_rw, host_access_gate
    from tools.filesystem_gate import AuthorizedPath

    async def allow_file_action(**_kwargs):
        return host_access_gate.HostActionAuthorization()

    async def allow_file_path(*, params, **_kwargs):
        return AuthorizedPath(Path(params["path"]), reviewed=False)

    monkeypatch.setattr(host_access_gate, "authorize_host_action", allow_file_action)
    monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", allow_file_path)
    monkeypatch.setattr(filesystem_edit, "_resolve_path_for_action", allow_file_path)
    workspace = tmp_path / "workspace"
    project_root = workspace / "projects" / "alpha"
    shared = project_root / "shared" / "planning"
    shared.mkdir(parents=True)
    (project_root / "shared" / "INDEX.md").write_text("# Shared\n", encoding="utf-8")
    (shared / "INDEX.md").write_text("# Planning\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(
        config,
        "WORKSPACE_SELECTION",
        WorkspaceSelection("managed-write-tests", workspace.resolve(), "Tests"),
    )
    monkeypatch.setattr(
        service, "get_allowed_dirs_rw", lambda **_kwargs: [workspace.resolve()]
    )
    db = get_db()
    db.execute(
        "INSERT INTO projects (id,name,path,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            "project-alpha",
            "Alpha",
            "workspace://managed-write-tests/projects/alpha",
            "active",
            "t",
            "t",
        ),
    )
    thread_id = make_session(db, project_id="project-alpha", prefix="completion")
    # A persistent agent may create and own shared files without naming an owner.
    db.execute("UPDATE threads SET agent_enabled=1 WHERE id=?", (thread_id,))
    db.commit()
    return workspace, project_root, thread_id, db


def _configure_source(db, thread_id, path, workspace) -> str:
    from core.context_source_paths import classify_context_source

    db.execute(
        "UPDATE threads SET bootloader_files=? WHERE id=?",
        (json.dumps([str(path)]), thread_id),
    )
    db.commit()
    changes = classify_context_source(path, workspace=workspace, db=db)
    assert len(changes) == 1, changes
    return changes[0].label


def _artifact_event_count(db) -> int:
    return db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0]


def _context_rows(db, thread_id, path) -> list:
    return db.execute(
        "SELECT details_json FROM context_change_journal "
        "WHERE object_kind='thread_context' AND object_id=? "
        "AND json_extract(details_json, '$.path')=?",
        (thread_id, path),
    ).fetchall()


def _start_turn(db, thread_id, turn_id):
    from core.turn_core import persist_turn_start
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    admission = admit_input(
        thread_id,
        role="user",
        content="test",
        intent="apply_now",
        message_id=f"{turn_id}:input",
        turn_id=turn_id,
        boot_epoch="test",
        admitted_at="t",
        db=db,
    )
    assert admission.batch is not None
    attempt_id = f"{turn_id}:write"
    begin_batch_write(admission.batch, attempt_id=attempt_id, started_at="t", db=db)
    settle_batch_write(attempt_id, outcome="accepted", db=db)
    return persist_turn_start(turn_id, thread_id, "native/test", "test", db=db)


async def _execute_registered_file_tool(
    *,
    workspace,
    thread_id,
    name,
    arguments,
):
    from infra.database import get_db
    from subagents.child_thread import persist_assistant_step
    from subagents.harness_tool_bridge import execute_tool
    from tools.filesystem_edit import EditFileTool, MultiEditTool
    from tools.filesystem_rw import WriteFileTool
    from tools.registry import ToolRegistry

    turn_id = f"turn-{uuid.uuid4().hex}"
    provider_call_id = f"provider-{uuid.uuid4().hex}"
    invocation_id = f"invocation-{uuid.uuid4().hex}"
    db = get_db()
    try:
        identity = _start_turn(db, thread_id, turn_id)
        step_id = persist_assistant_step(
            thread_id,
            content="",
            tool_uses=[{
                "id": provider_call_id,
                "name": name,
                "arguments": arguments,
            }],
            turn_id=turn_id,
            db=db,
        )
        db.commit()
    finally:
        db.close()

    registry = ToolRegistry()
    registry.register(WriteFileTool(workspace=workspace))
    registry.register(EditFileTool(workspace=workspace))
    registry.register(MultiEditTool(workspace=workspace))
    result = await execute_tool(
        registry,
        thread_id=thread_id,
        turn_identity=identity,
        invocation_id=invocation_id,
        provider_tool_call_id=provider_call_id,
        assistant_step_id=step_id,
        name=name,
        arguments=arguments,
    )
    return result, invocation_id, provider_call_id


def _managed_arguments(name, target):
    if name == "write_file":
        return {
            "path": str(target),
            "content": "after write",
            "description": "Guide",
            "update_trigger": "When the guide changes",
        }
    if name == "edit_file":
        return {"path": str(target), "old_string": "before", "new_string": "after"}
    return {
        "path": str(target),
        "edits": [{"old_string": "beta", "new_string": "delta"}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "initial", "expected"),
    [
        ("write_file", None, "after write"),
        ("edit_file", "before edit", "after edit"),
        ("multi_edit", "alpha beta", "alpha delta"),
    ],
)
async def test_managed_notice_failure_preserves_receipt_and_lineage(
    file_scope, name, initial, expected
):
    """A managed receipt retains exact partial-result bytes through persistence."""
    from infra.database import get_db
    from registry.durable_writes import write_text
    from registry.observer import observe_record

    workspace, project_root, thread_id, db = file_scope
    target = project_root / "shared" / "planning" / f"{name}.md"
    if initial is not None:
        write_text(
            target,
            initial,
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
            description="Guide",
            update_trigger="When the guide changes",
        )
    label = _configure_source(db, thread_id, target, workspace)
    baseline_events = _artifact_event_count(db)
    db.execute(
        "CREATE TRIGGER fail_context_notice "
        "BEFORE INSERT ON context_change_journal "
        "WHEN NEW.object_kind='thread_context' "
        "BEGIN SELECT RAISE(FAIL, 'injected context notice failure'); END"
    )
    db.commit()

    result, invocation_id, provider_call_id = await _execute_registered_file_tool(
        workspace=workspace,
        thread_id=thread_id,
        name=name,
        arguments=_managed_arguments(name, target),
    )
    receipt = result.delivery.content
    parsed = json.loads(receipt)

    assert target.read_text(encoding="utf-8") == expected
    assert parsed["registry_managed"] is True
    assert parsed["context_notice_error"] == _NOTICE_FAILURE
    assert result.delivery.invocation_id == invocation_id
    assert result.delivery.provider_tool_call_id == provider_call_id
    assert result.delivery.content_sha256 == hashlib.sha256(receipt.encode()).hexdigest()
    assert f'"invocation_id":"{invocation_id}"' in str(result)

    db = get_db()
    try:
        persisted = db.execute(
            "SELECT content,metadata FROM messages WHERE id=?",
            (result.delivery.message_id,),
        ).fetchone()
        assert persisted["content"] == receipt
        metadata = json.loads(persisted["metadata"])
        assert metadata["tool_invocation_id"] == invocation_id
        assert metadata["tool_call_id"] == provider_call_id
        events_before_observer = _artifact_event_count(db)
        assert events_before_observer > baseline_events
        observation = observe_record(
            thread_id,
            "tool",
            receipt,
            {
                "tool_call_id": provider_call_id,
                "assistant_step_id": result.delivery.assistant_step_id,
                "name": name,
            },
            db=db,
        )
        assert observation is not None
        assert observation.artifact is None
        assert observation.activity is not None
        assert _artifact_event_count(db) == events_before_observer
        # The document and its category INDEX.md, which gains a registered owner.
        assert db.execute("SELECT COUNT(*) FROM artifact_registrations").fetchone()[0] == 2
        assert _context_rows(db, thread_id, label) == []
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "initial", "expected"), [("write_file", None, "after write"), ("edit_file", "before", "after"), ("multi_edit", "alpha beta", "alpha delta")],
)
async def test_guided_writes_nudge_once_and_keep_guidance_receipt(file_scope, monkeypatch, name, initial, expected):
    """Guided saves notify open tabs without returning the raw write receipt."""
    from registry import service
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, db = file_scope
    target = project_root / "shared" / "planning" / f"guided-nudge-{name}.md"
    if initial is not None:
        write_text(target, initial, thread_id=thread_id, project_id="project-alpha", workspace=workspace, description="Guide", update_trigger="When the guide changes")
    _configure_source(db, thread_id, target, workspace)
    notifications = []

    def notify_changed(artifact):
        asyncio.get_running_loop()
        notifications.append(artifact)

    monkeypatch.setattr(service, "notify_changed", notify_changed)

    result, _invocation_id, _provider_call_id = await _execute_registered_file_tool(
        workspace=workspace, thread_id=thread_id, name=name, arguments=_managed_arguments(name, target),
    )
    receipt = json.loads(result.delivery.content)

    assert target.read_text(encoding="utf-8") == expected
    assert receipt["registry_managed"] is True
    # The caller returns history-decorated text, not the raw announced receipt.
    assert receipt["guidance_history"]
    assert notifications == [receipt["artifact"]]


@pytest.mark.asyncio
async def test_guided_write_without_registry_receipt_does_not_nudge(file_scope, monkeypatch):
    """An ungoverned guided write still has no artifact to announce."""
    from registry import service
    from tools.filesystem_rw import WriteFileTool

    workspace, project_root, _thread_id, _db = file_scope
    target = project_root / "shared" / "planning" / "unmanaged-guided-nudge.md"
    notifications = []
    monkeypatch.setattr(service, "notify_changed", notifications.append)

    result = await WriteFileTool(workspace=workspace).execute(
        path=str(target), content="after write", description="Guide", update_trigger="When the guide changes",
        reason="Exercise guidance writes", evidence="test",
    )

    assert target.read_text(encoding="utf-8") == "after write"
    assert "registry_managed" not in result
    assert notifications == []


@pytest.mark.asyncio
async def test_classifier_exception_returns_managed_partial_result(file_scope, monkeypatch):
    """Classification faults are visible, never recast as an ordinary path."""
    from core import context_source_paths

    workspace, project_root, thread_id, _db = file_scope
    target = project_root / "shared" / "planning" / "classifier-fault.md"
    label = _configure_source(_db, thread_id, target, workspace)

    def fail_classifier(*_args, **_kwargs):
        raise RuntimeError("injected classifier failure")

    monkeypatch.setattr(context_source_paths, "classify_context_source", fail_classifier)
    result, _invocation_id, _provider_call_id = await _execute_registered_file_tool(
        workspace=workspace,
        thread_id=thread_id,
        name="write_file",
        arguments=_managed_arguments("write_file", target),
    )
    receipt = json.loads(result.delivery.content)

    assert target.read_text(encoding="utf-8") == "after write"
    assert receipt["registry_managed"] is True
    assert receipt["context_notice_error"] == _NOTICE_FAILURE
    assert _context_rows(_db, thread_id, label) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "initial", "arguments", "expected"),
    [
        (
            "edit_file",
            "before",
            {"old_string": "before", "new_string": "after"},
            "after",
        ),
        (
            "multi_edit",
            "alpha beta",
            {"edits": [{"old_string": "beta", "new_string": "delta"}]},
            "alpha delta",
        ),
    ],
)
async def test_ordinary_edit_paths_keep_success_receipts(
    file_scope, name, initial, arguments, expected
):
    """Ordinary edit paths save bytes without a context-notice suffix."""
    workspace, project_root, thread_id, _db = file_scope
    target = project_root / ".threads" / thread_id / "working" / f"{name}.md"
    target.parent.mkdir(parents=True)
    target.write_text(initial, encoding="utf-8")

    result, _invocation_id, _provider_call_id = await _execute_registered_file_tool(
        workspace=workspace,
        thread_id=thread_id,
        name=name,
        arguments={"path": str(target), **arguments},
    )

    assert target.read_text(encoding="utf-8") == expected
    assert _NOTICE_FAILURE not in result.delivery.content
