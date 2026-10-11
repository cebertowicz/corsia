"""Reproduce missing notices from governed and ordinary configured file writes.

This is a unit-level receipt/observer seam, not proof of a live Native, Claude
Code, or Codex delivery. It drives the production tool classes, persists their
authoritative call records before execution, then supplies each exact receipt to
the production observer.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from infra.platform_paths import WorkspaceSelection
from tests.registry_helpers import make_session


@pytest.fixture
def context_scope(isolated_db, tmp_path, monkeypatch):
    """Create a disposable project with one valid governed shared category."""
    from infra import config
    from infra.database import get_db
    from registry import file_resolution, service

    workspace = tmp_path / "workspace"
    project_root = workspace / "projects" / "alpha"
    shared = project_root / "shared" / "planning"
    shared.mkdir(parents=True)
    (project_root / "shared" / "INDEX.md").write_text("# Shared\n", encoding="utf-8")
    (shared / "INDEX.md").write_text("# Planning\n", encoding="utf-8")
    selection = WorkspaceSelection(
        workspace_id="managed-context-workspace",
        root=workspace.resolve(),
        name="Managed context workspace",
    )
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", selection)
    monkeypatch.setattr(
        file_resolution,
        "get_allowed_dirs_rw",
        lambda **_kwargs: [workspace.resolve()],
    )
    db = get_db()
    db.execute(
        "INSERT INTO projects (id,name,path,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            "project-alpha",
            "Alpha",
            "workspace://managed-context-workspace/projects/alpha",
            "active",
            "t",
            "t",
        ),
    )
    thread_id = make_session(db, project_id="project-alpha", prefix="managed")
    # A persistent agent may create and own shared files without naming an owner.
    db.execute("UPDATE threads SET agent_enabled=1 WHERE id=?", (thread_id,))
    db.commit()
    return workspace, project_root, thread_id, db


def _configure_source(db, thread_id: str, target, workspace):
    """Configure a path through the same bootloader field the loader reads."""
    from core.context_source_paths import classify_context_source

    db.execute(
        "UPDATE threads SET bootloader_files=? WHERE id=?",
        (json.dumps([str(target)]), thread_id),
    )
    db.commit()
    changes = classify_context_source(target, workspace=workspace, db=db)
    assert len(changes) == 1, changes
    change = changes[0]
    assert (change.object_kind, change.object_id) == ("thread_context", thread_id)
    return change


def _persist_tool_call(db, thread_id: str, tool_call_id: str, name: str, args: dict) -> str:
    """Persist the authoritative assistant step that owns one tool call."""
    step_id = f"step-{uuid.uuid4().hex}"
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            step_id,
            thread_id,
            "assistant",
            "",
            json.dumps(
                {
                    "step_type": "tool_calls",
                    "llm_tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                }
            ),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()
    return step_id


def _artifact_event_count(db) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM artifact_registration_events"
    ).fetchone()[0]


def _context_rows(db, thread_id: str, label: str) -> list:
    return db.execute(
        "SELECT details_json FROM context_change_journal "
        "WHERE object_kind='thread_context' AND object_id=? "
        "AND json_extract(details_json, '$.path')=?",
        (thread_id, label),
    ).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "initial", "args", "expected"),
    [
        (
            "write_file",
            "before write",
            {"content": "after write", "description": "Boot guide"},
            "after write",
        ),
        (
            "edit_file",
            "before edit",
            {"old_string": "before", "new_string": "after"},
            "after edit",
        ),
        (
            "multi_edit",
            "alpha beta",
            {"edits": [{"old_string": "beta", "new_string": "delta"}]},
            "alpha delta",
        ),
    ],
)
async def test_managed_file_tool_receipt_journals_configured_context_source(
    context_scope,
    monkeypatch,
    name,
    initial,
    args,
    expected,
):
    """Each governed file tool must produce one configured-source notice."""
    from registry.durable_writes import write_text
    from registry.observer import observe_record
    from tools import filesystem_edit, filesystem_rw
    from tools.filesystem_edit import EditFileTool, MultiEditTool
    from tools.filesystem_gate import AuthorizedPath
    from tools.filesystem_rw import WriteFileTool

    workspace, project_root, thread_id, db = context_scope
    target = project_root / "shared" / "planning" / "context-guide.md"
    change = _configure_source(db, thread_id, target, workspace)
    write_text(
        target,
        initial,
        thread_id=thread_id,
        project_id="project-alpha",
        workspace=workspace,
        description="Boot guide",
        update_trigger="When the boot guide changes",
    )
    baseline_events = _artifact_event_count(db)
    # The document and its category INDEX.md, which gains a registered owner.
    assert baseline_events == 2
    call_id = f"{name}-{uuid.uuid4().hex}"
    call_args = {"path": str(target), **args}
    step_id = _persist_tool_call(db, thread_id, call_id, name, call_args)

    async def authorize(**_kwargs):
        return AuthorizedPath(target, reviewed=False)

    monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", authorize)
    monkeypatch.setattr(filesystem_edit, "_resolve_path_for_action", authorize)

    if name == "write_file":
        receipt = await WriteFileTool(workspace=workspace).execute(
            **call_args,
            _thread_id=thread_id,
            _thread_project_id="project-alpha",
        )
    elif name == "edit_file":
        receipt = await EditFileTool(workspace=workspace).execute(
            **call_args,
            _thread_id=thread_id,
            _thread_project_id="project-alpha",
        )
    else:
        receipt = await MultiEditTool(workspace=workspace).execute(
            **call_args,
            _thread_id=thread_id,
            _thread_project_id="project-alpha",
        )

    assert json.loads(receipt)["registry_managed"] is True
    assert target.read_text(encoding="utf-8") == expected
    events_before_observe = _artifact_event_count(db)
    assert events_before_observe > baseline_events
    observation = observe_record(
        thread_id,
        "tool",
        receipt,
        {
            "tool_call_id": call_id,
            "assistant_step_id": step_id,
            "name": name,
        },
        db=db,
    )
    assert observation is not None
    assert observation.artifact is None
    assert observation.activity is not None
    assert _artifact_event_count(db) == events_before_observe
    # The document and its category INDEX.md; the tool write adds neither.
    assert db.execute("SELECT COUNT(*) FROM artifact_identities").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM artifact_registrations").fetchone()[0] == 2

    rows = _context_rows(db, thread_id, change.label)
    assert len(rows) == 1
    assert json.loads(rows[0]["details_json"])["path"] == change.label


@pytest.mark.asyncio
async def test_unconfigured_ordinary_write_remains_a_non_context_control(
    context_scope,
    monkeypatch,
):
    """An ordinary file proves classification, not registration, controls notice."""
    from registry.observer import observe_record
    from tools import filesystem_rw
    from tools.filesystem_gate import AuthorizedPath
    from tools.filesystem_rw import WriteFileTool

    workspace, project_root, thread_id, db = context_scope
    target = project_root / ".threads" / thread_id / "working" / "ordinary.md"
    target.parent.mkdir(parents=True)
    call_id = f"ordinary-{uuid.uuid4().hex}"
    call_args = {"path": str(target), "content": "ordinary"}
    step_id = _persist_tool_call(db, thread_id, call_id, "write_file", call_args)

    async def authorize(**_kwargs):
        return AuthorizedPath(target, reviewed=False)

    monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", authorize)
    receipt = await WriteFileTool(workspace=workspace).execute(
        **call_args,
        _thread_id=thread_id,
        _thread_project_id="project-alpha",
    )

    assert receipt.startswith("Successfully wrote")
    assert target.read_text(encoding="utf-8") == "ordinary"
    observation = observe_record(
        thread_id,
        "tool",
        receipt,
        {
            "tool_call_id": call_id,
            "assistant_step_id": step_id,
            "name": "write_file",
        },
        db=db,
    )
    assert observation is not None
    assert observation.artifact is not None
    assert _context_rows(
        db,
        thread_id,
        f"projects/alpha/.threads/{thread_id}/working/ordinary.md",
    ) == []


@pytest.mark.asyncio
async def test_ordinary_configured_source_surfaces_context_notice_write_failure(
    context_scope,
    monkeypatch,
):
    """A journal fault must be visible rather than hidden behind a saved write."""
    from registry.observer import observe_record
    from tools import filesystem_rw
    from tools.filesystem_gate import AuthorizedPath
    from tools.filesystem_rw import WriteFileTool

    workspace, project_root, thread_id, db = context_scope
    target = (
        project_root / ".threads" / thread_id / "working" / "configured-fault.md"
    )
    target.parent.mkdir(parents=True)
    change = _configure_source(db, thread_id, target, workspace)
    db.execute(
        "CREATE TRIGGER fail_context_notice "
        "BEFORE INSERT ON context_change_journal "
        "WHEN NEW.object_kind='thread_context' "
        "BEGIN SELECT RAISE(FAIL, 'injected context notice failure'); END"
    )
    db.commit()
    call_id = f"fault-{uuid.uuid4().hex}"
    call_args = {"path": str(target), "content": "saved despite notice fault"}
    step_id = _persist_tool_call(db, thread_id, call_id, "write_file", call_args)

    async def authorize(**_kwargs):
        return AuthorizedPath(target, reviewed=False)

    monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", authorize)
    receipt = await WriteFileTool(workspace=workspace).execute(
        **call_args,
        _thread_id=thread_id,
        _thread_project_id="project-alpha",
    )
    observation = observe_record(
        thread_id,
        "tool",
        receipt,
        {
            "tool_call_id": call_id,
            "assistant_step_id": step_id,
            "name": "write_file",
        },
        db=db,
    )
    assert observation is not None
    observed = observation.artifact

    assert target.read_text(encoding="utf-8") == "saved despite notice fault"
    assert observed is not None
    assert _context_rows(db, thread_id, change.label) == []
    assert (
        "File saved. The context-change notice could not be recorded; other "
        "agents may still use earlier instructions." in receipt
    ), "saved configured-source write hid the injected context-notice failure"
