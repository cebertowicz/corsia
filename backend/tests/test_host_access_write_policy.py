"""Workspace write-policy gate against migrated logical-workspace state (#830).

Regression coverage for the post-cutover outage where every write_file /
edit_file / multi_edit call failed with the generic "Workspace write policy
could not be verified.": harness sessions build their tools with the session
working directory (a project directory) as the tool workspace, and the gate
passed that directory into durable-write scope resolution, which requires the
canonical selected workspace root once project rows store ``workspace://``
logical references.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from infra.platform_paths import WorkspaceSelection
from tests.registry_helpers import make_session


def _action(path: Path) -> SimpleNamespace:
    return SimpleNamespace(resolved_path=path)


@pytest.fixture
def migrated_scope(isolated_db, tmp_path, monkeypatch):
    """Workspace + project in the migrated logical-reference shape."""
    from infra import config
    from infra.database import get_db

    workspace = tmp_path / "workspace"
    project_root = workspace / "projects" / "alpha"
    (project_root / "skills" / "sdlc-process").mkdir(parents=True)
    (workspace / "context").mkdir()
    selection = WorkspaceSelection(
        workspace_id="workspace-stable-id",
        root=workspace.resolve(),
        name="Test workspace",
    )
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", selection)
    db = get_db()
    db.execute(
        "INSERT INTO projects (id,name,path,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            "project-alpha",
            "Alpha",
            "workspace://workspace-stable-id/projects/alpha",
            "active",
            "t",
            "t",
        ),
    )
    thread_id = make_session(db, project_id="project-alpha", prefix="wp")
    return workspace, project_root, thread_id, db


def _issue_830_targets(workspace: Path, project_root: Path) -> list[Path]:
    """The three write shapes that reproduced #830."""
    return [
        project_root / "skills" / "sdlc-process" / "SKILL.md",
        workspace / "context" / "scratch-write-test.md",
        project_root / "scratch-write-test.md",
    ]


@pytest.mark.asyncio
async def test_project_write_review_resolves_scope_from_canonical_root(
    migrated_scope,
):
    """Regression #830: review checks succeed for every repro shape.

    The gate previously resolved scope against the harness tool workspace
    (the project working directory); with a ``workspace://`` project row that
    crashed scope resolution on every call and the crash was swallowed into
    the generic fail-closed denial.
    """
    from tools import host_access_gate as gate

    workspace, project_root, thread_id, _db = migrated_scope

    for target in _issue_830_targets(workspace, project_root):
        requires_review = await gate._workspace_write_requires_review(
            action=_action(target),
            thread_id=thread_id,
            project_id="project-alpha",
            params={"path": str(target), "content": "x"},
        )
        # An ordinary agent writing outside its tray or shared/<category>
        # is routed to review — governed, never crashed.
        assert requires_review is True

    tray = project_root / ".threads" / thread_id / "working" / "notes.md"
    assert (
        await gate._workspace_write_requires_review(
            action=_action(tray),
            thread_id=thread_id,
            project_id="project-alpha",
            params={"path": str(tray), "content": "x"},
        )
        is False
    )


@pytest.mark.asyncio
async def test_conductor_structure_writes_flow_with_harness_working_dir(
    migrated_scope,
):
    """The project conductor keeps structure-owner rights on the same shapes."""
    from tools import host_access_gate as gate

    workspace, project_root, thread_id, db = migrated_scope
    db.execute(
        "UPDATE projects SET conductor_thread_id=? WHERE id='project-alpha'",
        (thread_id,),
    )
    db.commit()

    skill_file, workspace_context, project_scratch = _issue_830_targets(
        workspace, project_root
    )
    for owned in (skill_file, project_scratch):
        assert (
            await gate._workspace_write_requires_review(
                action=_action(owned),
                thread_id=thread_id,
                project_id="project-alpha",
                params={"path": str(owned), "content": "x"},
            )
            is False
        )
    # A workspace-level file stays outside the project scope even for the
    # project conductor: still routed to review, not crashed.
    assert (
        await gate._workspace_write_requires_review(
            action=_action(workspace_context),
            thread_id=thread_id,
            project_id="project-alpha",
            params={"path": str(workspace_context), "content": "x"},
        )
        is True
    )


@pytest.mark.asyncio
async def test_authorize_write_with_harness_working_dir_tool(
    migrated_scope,
    monkeypatch,
):
    """End-to-end #830 shape: authorize_host_action for a write through a
    harness-bridge tool whose workspace is the session working directory."""
    from tools import host_access_gate as gate

    workspace, project_root, thread_id, _db = migrated_scope
    monkeypatch.setattr(gate, "is_path_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(gate, "check_repo_write_path", lambda _path: None)

    async def no_lease(_thread_id):
        return None

    monkeypatch.setattr(gate, "_resolve_active_lease", no_lease)
    harness_tool = SimpleNamespace(
        _workspace=project_root,
        _allowed_dir=None,
    )

    tool_params = [
        ("write_file", {"content": "body"}),
        ("edit_file", {"old_string": "a", "new_string": "b"}),
        (
            "multi_edit",
            {"edits": [{"old_string": "a", "new_string": "b"}]},
        ),
    ]
    targets = _issue_830_targets(workspace, project_root)
    for target, (tool_name, extra) in zip(
        targets, tool_params, strict=True
    ):
        authorization = await gate.authorize_host_action(
            thread_id=thread_id,
            project_id="project-alpha",
            tool=harness_tool,
            tool_name=tool_name,
            params={"path": str(target), **extra},
        )
        assert authorization.parameter_overrides == {}


@pytest.mark.asyncio
async def test_internal_fault_is_named_with_cause_not_generic(
    migrated_scope,
    monkeypatch,
):
    """An internal fault names the check, target, and cause — never the old
    generic policy-denial sentence."""
    import registry.shared_documents as shared_documents
    from tools import host_access_gate as gate

    workspace, project_root, thread_id, _db = migrated_scope

    def explode(*_args, **_kwargs):
        raise RuntimeError("scope table exploded")

    monkeypatch.setattr(
        shared_documents, "workspace_write_requires_review", explode
    )
    target = project_root / "scratch-write-test.md"

    with pytest.raises(gate.HostAccessBlocked) as raised:
        await gate._workspace_write_requires_review(
            action=_action(target),
            thread_id=thread_id,
            project_id="project-alpha",
            params={"path": str(target), "content": "x"},
        )

    message = str(raised.value)
    assert "failed internally" in message
    assert "not a policy denial" in message
    assert str(target) in message
    assert "RuntimeError: scope table exploded" in message
    assert message != "Workspace write policy could not be verified."
    assert isinstance(raised.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_internal_fault_detail_is_single_line_and_bounded(
    migrated_scope,
    monkeypatch,
):
    """The model-visible fault detail is sanitized: one line, bounded length."""
    import registry.shared_documents as shared_documents
    from tools import host_access_gate as gate

    _workspace, project_root, thread_id, _db = migrated_scope

    def explode(*_args, **_kwargs):
        raise RuntimeError("line one\nline two\n" + "x" * 600)

    monkeypatch.setattr(
        shared_documents, "workspace_write_requires_review", explode
    )
    target = project_root / "scratch-write-test.md"

    with pytest.raises(gate.HostAccessBlocked) as raised:
        await gate._workspace_write_requires_review(
            action=_action(target),
            thread_id=thread_id,
            project_id="project-alpha",
            params={"path": str(target), "content": "x"},
        )

    message = str(raised.value)
    assert "\n" not in message
    assert "RuntimeError: line one line two" in message
    assert message.endswith("…")
    # The embedded detail respects the sanitizer's 300-character bound.
    detail = message.split("denial): ", 1)[1]
    assert len(detail) <= len("RuntimeError: ") + 300


def test_selected_root_mismatch_reports_observed_and_expected(
    migrated_scope,
):
    """Scope resolution against a non-root directory fails loud with both
    the observed argument and the selected root."""
    from infra.workspace_paths import workspace_selection_for_root

    workspace, project_root, _thread_id, _db = migrated_scope

    with pytest.raises(ValueError) as raised:
        workspace_selection_for_root(project_root)

    message = str(raised.value)
    assert str(project_root.resolve()) in message
    assert str(workspace.resolve()) in message


def test_unreviewed_illegitimate_write_is_refused_with_specific_reason(
    migrated_scope,
):
    """Governance is not weakened: the write layer still refuses an ordinary
    agent's out-of-tray write with the specific policy reason."""
    from registry.durable_writes import write_text
    from registry.shared_write_errors import DurableWritePolicyError

    workspace, project_root, thread_id, _db = migrated_scope
    target = project_root / "skills" / "sdlc-process" / "SKILL.md"

    with pytest.raises(DurableWritePolicyError, match="scope structure owned by"):
        write_text(
            target,
            "content",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
        )
    assert not target.exists()
