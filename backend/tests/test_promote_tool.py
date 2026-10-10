"""Promotion into approved shared categories through the durable-write seam."""

from __future__ import annotations

import json
import uuid

import pytest

from tools.grep import GrepTool
from tools.promote import PromoteArtifactTool
from auth_fixtures import OWNER_TOKEN

pytestmark = pytest.mark.integration

_TOKEN = OWNER_TOKEN


@pytest.fixture(autouse=True)
def _registrable_roots(integration_workspace, monkeypatch):
    from tools import sandbox_config

    monkeypatch.setattr(
        sandbox_config,
        "_STATIC_ALLOWED_RW",
        [integration_workspace.resolve()],
    )


def _mk_project(client) -> tuple[str, str]:
    name = f"Promote Project {uuid.uuid4().hex[:6]}"
    response = client.post(f"/api/projects?token={_TOKEN}", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["project"]["id"], name.lower().replace(" ", "-")


def _mk_session(client, *, project_id: str | None = None) -> str:
    from infra.database import get_db

    body = {"title": f"promote-{uuid.uuid4().hex[:6]}"}
    if project_id:
        body["project_id"] = project_id
    response = client.post(f"/api/threads?token={_TOKEN}", json=body)
    assert response.status_code == 200, response.text
    thread_id = response.json()["thread_id"]
    db = get_db()
    db.execute(
        "UPDATE threads SET agent_enabled=1,agent_responsibility='Own promotions' "
        "WHERE id=?",
        (thread_id,),
    )
    db.commit()
    db.close()
    return thread_id


def _scope_root(workspace, slug: str | None):
    return workspace / "projects" / slug if slug else workspace


def _approve_category(workspace, slug: str | None, category: str = "planning"):
    shared = _scope_root(workspace, slug) / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "INDEX.md").write_text("# Shared\n", encoding="utf-8")
    category_root = shared / category
    category_root.mkdir()
    (category_root / "INDEX.md").write_text(
        f"# {category.title()}\n",
        encoding="utf-8",
    )
    return category_root


def _working_file(workspace, slug, thread_id, name, data: bytes):
    path = (
        _scope_root(workspace, slug)
        / ".threads"
        / thread_id
        / "working"
        / name
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


async def _promote(
    tool,
    source,
    thread_id,
    *,
    category="planning",
    turn_identity=None,
    update_trigger="when source facts change",
):
    return await tool.execute(
        path=str(source),
        category=category,
        description="Durable output for future threads.",
        update_trigger=update_trigger,
        _thread_id=thread_id,
        _host_access_turn_identity=turn_identity,
    )


async def test_promote_moves_registers_and_makes_discoverable(
    integration_client,
    integration_workspace,
):
    from core.turn_core import TurnIdentity
    from infra.database import get_db
    from plans import service as plan_service
    from registry import service

    project_id, slug = _mk_project(integration_client)
    thread_id = _mk_session(integration_client, project_id=project_id)
    db = get_db()
    plan = plan_service.ensure_plan("thread", thread_id, db=db)
    node = plan_service.add_node(
        plan.id,
        kind="task",
        title="Promote artifact",
        acceptance_criteria="Shared artifact is registered",
        db=db,
    )
    node = plan_service.transition_node(node.id, "active", db=db)
    turn = TurnIdentity("promote-tool-turn", 1)
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
            plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role)
           VALUES (?,?,0,'native/test','running','t0','boot',?,?,?,?,?)""",
        (
            turn.turn_id,
            thread_id,
            plan.id,
            node.id,
            "attempt-promote-tool",
            node.commitment_id,
            "execution",
        ),
    )
    db.commit()
    category = _approve_category(integration_workspace, slug)
    source = _working_file(
        integration_workspace,
        slug,
        thread_id,
        "finding.md",
        b"PROMOTABLE audit result",
    )

    result = await _promote(
        PromoteArtifactTool(workspace=integration_workspace),
        source,
        thread_id,
        turn_identity=turn,
    )
    receipt = json.loads(result)
    destination = category / "finding.md"

    assert receipt["ok"] is True
    assert receipt["registry_managed"] is True
    assert not source.exists()
    assert destination.read_bytes() == b"PROMOTABLE audit result"
    artifact = service.find_file_identity(str(destination))
    assert artifact is not None
    assert artifact["external_id"].startswith("workspace://")
    assert artifact["path"] == str(destination.resolve())
    registration = db.execute(
        """SELECT origin_plan_node_id FROM artifact_registrations
           WHERE artifact_id=? AND parent_thread_id=?""",
        (artifact["id"], thread_id),
    ).fetchone()
    assert registration["origin_plan_node_id"] == node.id
    event = db.execute(
        """SELECT metadata FROM artifact_registration_events
           WHERE artifact_id=? AND event_type='registered'
           ORDER BY rowid DESC LIMIT 1""",
        (artifact["id"],),
    ).fetchone()
    assert json.loads(event["metadata"])["producer"] == "promote_artifact"

    other = _mk_session(integration_client, project_id=project_id)
    hits = await GrepTool(workspace=integration_workspace).execute(
        pattern="PROMOTABLE",
        output_mode="files",
        _thread_id=other,
    )
    assert f"projects/{slug}/shared/planning/finding.md" in hits


async def test_promote_requires_update_trigger_for_agent_created_shared_file(
    integration_client,
    integration_workspace,
):
    project_id, slug = _mk_project(integration_client)
    thread_id = _mk_session(integration_client, project_id=project_id)
    _approve_category(integration_workspace, slug)
    source = _working_file(
        integration_workspace,
        slug,
        thread_id,
        "missing-trigger.md",
        b"needs trigger",
    )

    result = await _promote(
        PromoteArtifactTool(workspace=integration_workspace),
        source,
        thread_id,
        update_trigger=None,
    )

    assert result.startswith("Error: An `update_trigger` is required")
    assert source.exists()


async def test_promote_with_harness_working_dir_tool_workspace(
    integration_client,
    integration_workspace,
):
    """Regression #830 family: a harness session's tool workspace is its
    working directory (the project directory), and durable-write governance
    must still resolve scope from the canonical workspace root."""
    project_id, slug = _mk_project(integration_client)
    thread_id = _mk_session(integration_client, project_id=project_id)
    category = _approve_category(integration_workspace, slug)
    source = _working_file(
        integration_workspace,
        slug,
        thread_id,
        "bridge-finding.md",
        b"bridge promotable",
    )

    result = await _promote(
        PromoteArtifactTool(workspace=_scope_root(integration_workspace, slug)),
        source,
        thread_id,
    )
    receipt = json.loads(result)

    assert receipt["ok"] is True
    assert not source.exists()
    assert (category / "bridge-finding.md").read_bytes() == b"bridge promotable"


async def test_promote_rejects_outside_exact_working_tray(
    integration_client,
    integration_workspace,
):
    project_id, slug = _mk_project(integration_client)
    thread_id = _mk_session(integration_client, project_id=project_id)
    _approve_category(integration_workspace, slug)
    source = _scope_root(integration_workspace, slug) / "context" / "note.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("not scratch", encoding="utf-8")

    result = await _promote(
        PromoteArtifactTool(workspace=integration_workspace),
        source,
        thread_id,
    )

    assert result.startswith("Error")
    assert "exact owning" in result
    assert source.exists()


async def test_promote_requires_approved_category_and_description(
    integration_client,
    integration_workspace,
):
    project_id, slug = _mk_project(integration_client)
    thread_id = _mk_session(integration_client, project_id=project_id)
    source = _working_file(
        integration_workspace,
        slug,
        thread_id,
        "notes.md",
        b"notes",
    )
    tool = PromoteArtifactTool(workspace=integration_workspace)

    missing = await tool.execute(
        path=str(source),
        category="research",
        description="Research notes",
        _thread_id=thread_id,
    )
    _approve_category(integration_workspace, slug, "research")
    no_description = await tool.execute(
        path=str(source),
        category="research",
        description="",
        _thread_id=thread_id,
    )

    assert missing.startswith("Error") and "not approved" in missing
    assert no_description.startswith("Error") and "description" in no_description
    assert source.exists()


async def test_promote_rolls_back_when_registration_fails(
    integration_client,
    integration_workspace,
    monkeypatch,
):
    from registry import service

    project_id, slug = _mk_project(integration_client)
    thread_id = _mk_session(integration_client, project_id=project_id)
    category = _approve_category(integration_workspace, slug)
    source = _working_file(
        integration_workspace,
        slug,
        thread_id,
        "rollback.md",
        b"DATA",
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(service, "register_write_strict", fail)
    result = await _promote(
        PromoteArtifactTool(workspace=integration_workspace),
        source,
        thread_id,
    )

    assert result.startswith("Error") and "db exploded" in result
    assert source.read_bytes() == b"DATA"
    assert not (category / "rollback.md").exists()


async def test_promote_claim_preserves_source_recreated_during_publication(
    integration_client,
    integration_workspace,
    monkeypatch,
):
    from registry import durable_writes, models, service

    project_id, slug = _mk_project(integration_client)
    thread_id = _mk_session(integration_client, project_id=project_id)
    category = _approve_category(integration_workspace, slug)
    source = _working_file(
        integration_workspace,
        slug,
        thread_id,
        "concurrent.md",
        b"CLAIMED VERSION",
    )
    source_identity = service.register_write_strict(thread_id, str(source))
    original_claim = durable_writes.claim_bound_file

    def claim_then_recreate(source_path, claim_path):
        content = original_claim(source_path, claim_path)
        source_path.write_bytes(b"NEWER VERSION")
        return content

    monkeypatch.setattr(durable_writes, "claim_bound_file", claim_then_recreate)
    result = await _promote(
        PromoteArtifactTool(workspace=integration_workspace),
        source,
        thread_id,
    )

    assert json.loads(result)["ok"] is True
    assert (category / "concurrent.md").read_bytes() == b"CLAIMED VERSION"
    assert source.read_bytes() == b"NEWER VERSION"
    assert models.get_artifact(source_identity["id"])["state"] == "active"
    assert list(source.parent.glob(".corsia-write-*.source")) == []


async def test_failed_promote_preserves_claim_and_newer_source(
    integration_client,
    integration_workspace,
    monkeypatch,
):
    from registry import durable_writes, service

    project_id, slug = _mk_project(integration_client)
    thread_id = _mk_session(integration_client, project_id=project_id)
    category = _approve_category(integration_workspace, slug)
    source = _working_file(
        integration_workspace,
        slug,
        thread_id,
        "failed-concurrent.md",
        b"CLAIMED VERSION",
    )
    original_claim = durable_writes.claim_bound_file

    def claim_then_recreate(source_path, claim_path):
        claimed = original_claim(source_path, claim_path)
        source_path.write_bytes(b"NEWER VERSION")
        return claimed

    def fail(*_args, **_kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(durable_writes, "claim_bound_file", claim_then_recreate)
    monkeypatch.setattr(service, "register_write_strict", fail)
    result = await _promote(
        PromoteArtifactTool(workspace=integration_workspace),
        source,
        thread_id,
    )
    recovered = next(source.parent.glob("failed-concurrent.md.promotion-recovery-*"))

    assert result.startswith("Error") and "db exploded" in result
    assert source.read_bytes() == b"NEWER VERSION"
    assert recovered.read_bytes() == b"CLAIMED VERSION"
    assert not (category / "failed-concurrent.md").exists()
    assert list(source.parent.glob(".corsia-write-*.source")) == []


async def test_promote_preserves_source_mode(
    integration_client,
    integration_workspace,
):
    project_id, slug = _mk_project(integration_client)
    thread_id = _mk_session(integration_client, project_id=project_id)
    category = _approve_category(integration_workspace, slug)
    source = _working_file(
        integration_workspace,
        slug,
        thread_id,
        "script.sh",
        b"#!/bin/sh\n",
    )
    source.chmod(0o750)

    result = await _promote(
        PromoteArtifactTool(workspace=integration_workspace),
        source,
        thread_id,
    )

    assert json.loads(result)["ok"] is True
    assert (category / "script.sh").stat().st_mode & 0o777 == 0o750


async def test_promote_preserves_registered_source_identity(
    integration_client,
    integration_workspace,
):
    from registry import service

    project_id, slug = _mk_project(integration_client)
    thread_id = _mk_session(integration_client, project_id=project_id)
    category = _approve_category(integration_workspace, slug)
    source = _working_file(
        integration_workspace,
        slug,
        thread_id,
        "registered.md",
        b"registered",
    )
    source_identity = service.register_write_strict(thread_id, str(source))

    result = await _promote(
        PromoteArtifactTool(workspace=integration_workspace),
        source,
        thread_id,
    )

    assert json.loads(result)["ok"] is True
    destination = service.find_file_identity(str(category / "registered.md"))
    assert destination is not None
    assert destination["id"] == source_identity["id"]
    assert destination["state"] == "active"


async def test_promote_exact_collision_fails_without_overwrite_escape(
    integration_client,
    integration_workspace,
):
    project_id, slug = _mk_project(integration_client)
    thread_id = _mk_session(integration_client, project_id=project_id)
    category = _approve_category(integration_workspace, slug)
    tool = PromoteArtifactTool(workspace=integration_workspace)
    first = _working_file(
        integration_workspace,
        slug,
        thread_id,
        "same.md",
        b"FIRST",
    )
    assert json.loads(await _promote(tool, first, thread_id))["ok"]
    second = _working_file(
        integration_workspace,
        slug,
        thread_id,
        "same.md",
        b"SECOND",
    )

    result = await _promote(tool, second, thread_id)

    assert result.startswith("Error") and "already exists" in result
    assert second.read_bytes() == b"SECOND"
    assert (category / "same.md").read_bytes() == b"FIRST"


async def test_workspace_scoped_thread_can_promote(
    integration_client,
    integration_workspace,
):
    thread_id = _mk_session(integration_client)
    category = _approve_category(integration_workspace, None, "reference")
    source = _working_file(
        integration_workspace,
        None,
        thread_id,
        "workspace.md",
        b"workspace scope",
    )

    result = await _promote(
        PromoteArtifactTool(workspace=integration_workspace),
        source,
        thread_id,
        category="reference",
    )

    assert json.loads(result)["ok"] is True
    assert (category / "workspace.md").read_bytes() == b"workspace scope"


async def test_promote_preserves_binary_bytes(
    integration_client,
    integration_workspace,
):
    project_id, slug = _mk_project(integration_client)
    thread_id = _mk_session(integration_client, project_id=project_id)
    category = _approve_category(integration_workspace, slug)
    source = _working_file(
        integration_workspace,
        slug,
        thread_id,
        "evidence.bin",
        b"\x00\xff\x10",
    )

    result = await _promote(
        PromoteArtifactTool(workspace=integration_workspace),
        source,
        thread_id,
    )

    assert json.loads(result)["ok"] is True
    assert (category / "evidence.bin").read_bytes() == b"\x00\xff\x10"
