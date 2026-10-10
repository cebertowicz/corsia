from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

import pytest

from infra.platform_paths import WorkspaceSelection
from tests.registry_helpers import make_session


@pytest.fixture
def freshness_scope(isolated_db, tmp_path, monkeypatch):
    from infra import config
    from infra.database import get_db
    from registry import service
    from tools import sandbox_config

    workspace = tmp_path / "workspace"
    root = workspace / "projects" / "alpha"
    category = root / "shared" / "documents"
    category.mkdir(parents=True)
    (root / "shared" / "INDEX.md").write_text("# Shared\n", encoding="utf-8")
    (category / "INDEX.md").write_text("# Documents\n", encoding="utf-8")
    selection = WorkspaceSelection(
        workspace_id="workspace-stable-id",
        root=workspace.resolve(),
        name="Test workspace",
    )
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", selection)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
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
            "workspace://workspace-stable-id/projects/alpha",
            "active",
            "t",
            "t",
        ),
    )
    owner = make_session(
        db, project_id="project-alpha", title="Documents & Artifacts", prefix="owner"
    )
    other = make_session(
        db, project_id="project-alpha", title="Other Lane", prefix="other"
    )
    conductor = make_session(
        db, project_id="project-alpha", title="Conductor", prefix="conductor"
    )
    worker = make_session(
        db, project_id="project-alpha", title="Worker", prefix="worker"
    )
    foreign = make_session(
        db,
        project_id="project-alpha",
        title="Foreign",
        prefix="foreign",
        user_id="leksi",
    )
    db.execute(
        "UPDATE threads SET agent_enabled=1 WHERE id IN (?,?,?,?)",
        (owner, other, conductor, foreign),
    )
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", (owner, worker))
    db.execute(
        "UPDATE projects SET conductor_thread_id=? WHERE id='project-alpha'",
        (conductor,),
    )
    db.commit()
    yield {
        "workspace": workspace,
        "category": category,
        "db": db,
        "owner": owner,
        "other": other,
        "conductor": conductor,
        "worker": worker,
        "foreign": foreign,
    }
    db.close()


def _register(scope, name: str, content: str = "# Document\n\nBody.\n", *, owner=None):
    from registry import service

    path = scope["category"] / name
    path.write_text(content, encoding="utf-8")
    artifact = service.register_write_strict(
        owner or scope["owner"],
        str(path),
        metadata={"update_trigger": "when source facts change"},
        db=scope["db"],
    )
    return path, artifact


def test_mark_verified_allows_owner_worker_conductor_and_user_but_names_owner_on_refusal(
    freshness_scope,
):
    from registry.doc_freshness import mark_verified
    from registry.identity_metadata import parse_metadata
    from registry.shared_principals import UI_PRINCIPAL

    scope = freshness_scope
    _path, artifact = _register(scope, "permissions.md")
    moments = [
        datetime(2026, 9, 26 + index, tzinfo=timezone.utc)
        for index in range(4)
    ]
    principals = [
        scope["owner"], scope["worker"], scope["conductor"], UI_PRINCIPAL,
    ]
    for principal, moment in zip(principals, moments, strict=True):
        row = mark_verified(artifact["id"], principal, db=scope["db"], now=moment)
        metadata = parse_metadata(row["metadata"])
        assert metadata["last_verified_at"] == moment.isoformat()
        assert metadata["last_verified_by"] == principal

    with pytest.raises(PermissionError, match="owned by Documents & Artifacts"):
        mark_verified(artifact["id"], scope["other"], db=scope["db"])


def test_edit_preserves_verification_and_header_seed_is_idempotent_and_rejects_sha(
    freshness_scope,
):
    from registry import service
    from registry.doc_freshness import mark_verified, seed_verified_from_headers
    from registry.durable_writes import write_text
    from registry.identity_metadata import parse_metadata

    scope = freshness_scope
    path, artifact = _register(
        scope,
        "seed.md",
        "# Seed\n**Status:** current · **Verified at:** 2026-09-12, commit abc · **Review:** facts change\n\nBody.\n",
    )
    assert seed_verified_from_headers(artifact["id"], db=scope["db"]) is True
    assert seed_verified_from_headers(artifact["id"], db=scope["db"]) is False
    seeded = service.find_file_identity(str(path), db=scope["db"])
    assert parse_metadata(seeded["metadata"])["last_verified_at"] == (
        "2026-09-12T00:00:00+00:00"
    )

    verified_at = datetime(2026, 9, 29, tzinfo=timezone.utc)
    mark_verified(artifact["id"], scope["owner"], db=scope["db"], now=verified_at)
    write_text(
        path,
        "# Seed\n\nEdited body.\n",
        thread_id=scope["owner"],
        project_id="project-alpha",
        workspace=scope["workspace"],
        description="Edited document",
        update_trigger="when source facts change",
    )
    edited = service.find_file_identity(str(path), db=scope["db"])
    assert parse_metadata(edited["metadata"])["last_verified_at"] == (
        verified_at.isoformat()
    )

    _sha_path, sha_artifact = _register(
        scope,
        "sha.md",
        "# SHA\n**Status:** current · **Verified at:** abcdef123456 · **Review:** facts change\n\nBody.\n",
    )
    assert seed_verified_from_headers(sha_artifact["id"], db=scope["db"]) is False


@pytest.mark.asyncio
async def test_artifact_update_exposes_mark_verified_and_refuses_non_owner(
    freshness_scope,
):
    from registry.identity_metadata import parse_metadata
    from tools.artifacts import ArtifactUpdateTool

    scope = freshness_scope
    _path, artifact = _register(scope, "tool-verify.md")
    tool = ArtifactUpdateTool()

    assert tool.parameters["properties"]["mark_verified"]["type"] == [
        "boolean", "null",
    ]
    allowed = json.loads(await tool.execute(
        artifact["id"], mark_verified=True, _thread_id=scope["owner"]
    ))
    assert parse_metadata(allowed["artifact"]["metadata"])["last_verified_by"] == (
        scope["owner"]
    )
    denied = await tool.execute(
        artifact["id"], mark_verified=True, _thread_id=scope["other"]
    )
    assert "owned by Documents & Artifacts" in denied


@pytest.mark.asyncio
async def test_read_annotation_is_outside_numbered_content_and_only_on_first_page(
    freshness_scope,
):
    from registry.doc_freshness import mark_verified
    from tools.filesystem_rw import ReadFileTool

    scope = freshness_scope
    path, artifact = _register(scope, "read.md")
    now = datetime.now(timezone.utc)
    mark_verified(
        artifact["id"], scope["owner"], db=scope["db"],
        now=now - timedelta(days=94),
    )
    tool = ReadFileTool(
        workspace=scope["workspace"], allowed_dir=scope["workspace"]
    )

    first = await tool.execute(str(path), _thread_id=scope["other"])
    later = await tool.execute(str(path), offset=2, _thread_id=scope["other"])
    ordinary = scope["workspace"] / "ordinary.md"
    ordinary.write_text("ordinary\n", encoding="utf-8")
    plain = await tool.execute(str(ordinary), _thread_id=scope["other"])

    lines = first.splitlines()
    assert lines[0] == (
        "[Shared document · Owner: Documents & Artifacts · verified 94 days ago "
        "· review overdue] Its owner clears this after checking it against current "
        f"sources with `artifact_update(artifact_id=\"{artifact['id']}\", "
        "mark_verified=true)`; editing its Verified at header does not count."
    )
    assert lines[1].startswith("     1\t# Document")
    assert "[Shared document" not in later
    assert "[Shared document" not in plain
    copied_old_string = "\n".join(
        line.split("\t", 1)[1] for line in lines if re.match(r"^\s+\d+\t", line)
    )
    assert copied_old_string.startswith("# Document")
    assert "Shared document" not in copied_old_string


@pytest.mark.asyncio
async def test_artifact_list_filters_pages_hides_foreign_rows_and_dispatches_before_load(
    freshness_scope,
):
    from infra import state
    from registry.doc_freshness import mark_verified
    from subagents.harness_tool_bridge import build_tool_registry
    from tools.artifact_list import ArtifactListTool

    scope = freshness_scope
    for index in range(51):
        path, artifact = _register(scope, f"owned-{index:02d}.md")
        if index == 0:
            mark_verified(
                artifact["id"], scope["owner"], db=scope["db"],
                now=datetime.now(timezone.utc) - timedelta(days=31),
            )
    _register(scope, "foreign.md", owner=scope["foreign"])

    tool = ArtifactListTool(workspace=scope["workspace"])
    first = json.loads(await tool.execute(owned=True, _thread_id=scope["owner"]))
    second = json.loads(await tool.execute(
        owned=True, cursor=first["next_cursor"], _thread_id=scope["owner"]
    ))
    overdue = json.loads(await tool.execute(
        owned=True, state="overdue", _thread_id=scope["owner"]
    ))
    accessible = json.loads(await tool.execute(
        owned=False, _thread_id=scope["owner"]
    ))

    assert len(first["rows"]) == 50
    assert len(second["rows"]) == 1
    assert first["total"] == 51
    assert accessible["total"] == 51
    assert overdue["total"] == 1
    assert overdue["rows"][0]["update_trigger"] == "when source facts change"
    assert all(row["display_name"] != "foreign.md" for row in accessible["rows"])

    registry = build_tool_registry(str(scope["workspace"]))
    reset = state._current_thread_id.set(scope["owner"])
    try:
        dispatched = await registry.execute("tool_call", {
            "name": "artifact_list",
            "arguments": {"owned": True, "state": "never_verified"},
        })
    finally:
        state._current_thread_id.reset(reset)
    assert json.loads(dispatched)["total"] == 50


@pytest.mark.asyncio
async def test_artifact_list_works_from_a_project_thread_workspace(freshness_scope):
    """A project thread's tools are built on its project root, not the workspace root."""
    from infra import state
    from subagents.harness_tool_bridge import build_tool_registry
    from tools.artifact_list import ArtifactListTool

    scope = freshness_scope
    _register(scope, "project-owned.md")
    project_root = scope["workspace"] / "projects" / "alpha"

    listed = json.loads(await ArtifactListTool(workspace=project_root).execute(
        owned=True, _thread_id=scope["owner"],
    ))
    assert listed["total"] == 1

    registry = build_tool_registry(str(project_root))
    reset = state._current_thread_id.set(scope["owner"])
    try:
        dispatched = await registry.execute("tool_call", {
            "name": "artifact_list",
            "arguments": {"owned": True, "state": "never_verified"},
        })
    finally:
        state._current_thread_id.reset(reset)
    assert json.loads(dispatched)["total"] == 1


@pytest.mark.asyncio
async def test_unverified_read_names_the_one_action_that_clears_it(freshness_scope):
    """#3215: a header Verified at is not verification; the banner says what is."""
    from registry.doc_freshness import mark_verified
    from tools.filesystem_rw import ReadFileTool

    scope = freshness_scope
    path, artifact = _register(
        scope,
        "header-only.md",
        "# Header only\n**Status:** current · **Verified at:** 2026-09-30 · **Review:** monthly\n",
    )
    tool = ReadFileTool(workspace=scope["workspace"], allowed_dir=scope["workspace"])

    unverified = (await tool.execute(str(path), _thread_id=scope["owner"])).splitlines()[0]
    assert "never verified]" in unverified
    assert f'artifact_update(artifact_id="{artifact["id"]}", mark_verified=true)' in unverified

    mark_verified(artifact["id"], scope["owner"], db=scope["db"])
    verified = (await tool.execute(str(path), _thread_id=scope["owner"])).splitlines()[0]
    assert verified == "[Shared document · Owner: Documents & Artifacts · verified 0 days ago]"
