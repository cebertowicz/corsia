from __future__ import annotations

from pathlib import Path
import uuid

import pytest
from auth_fixtures import OWNER_TOKEN, OTHER_TOKEN

pytestmark = pytest.mark.integration

_TOKEN = OWNER_TOKEN
_OTHER_TOKEN = OTHER_TOKEN


def _skill_text(name: str, description: str = "Use when this skill applies.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n"


def _mk_session(client, *, project_id: str | None = None) -> str:
    body: dict[str, str] = {"title": f"skills-{uuid.uuid4().hex[:6]}"}
    if project_id:
        body["project_id"] = project_id
    res = client.post(f"/api/threads?token={_TOKEN}", json=body)
    assert res.status_code == 200, res.text
    return res.json()["thread_id"]


def _mk_project(client, name: str) -> str:
    res = client.post(f"/api/projects?token={_TOKEN}", json={"name": name})
    assert res.status_code == 200, res.text
    return res.json()["project"]["id"]


def test_create_workspace_skill_and_list_legacy_global_inventory(integration_client, integration_workspace):
    name = f"api-skill-{uuid.uuid4().hex[:8]}"

    created = integration_client.post(
        f"/api/skills?token={_TOKEN}",
        json={"scope": "workspace", "name": name, "description": "Use from the workspace."},
    )

    assert created.status_code == 200, created.text
    skill = created.json()["skill"]
    path = Path(skill["path"])
    assert path == integration_workspace / "skills" / name / "SKILL.md"
    assert path.exists()
    assert skill["status"] == "included"

    listed = integration_client.get(f"/api/skills?token={_TOKEN}&scope=global")
    assert listed.status_code == 200, listed.text
    rows = listed.json()["skills"]
    assert listed.json()["scope"] == "workspace"
    assert any(row["name"] == name and row["scope"] == "workspace" for row in rows)


def test_create_skill_rejects_duplicate_local_name(integration_client):
    name = f"dup-skill-{uuid.uuid4().hex[:8]}"
    first = integration_client.post(
        f"/api/skills?token={_TOKEN}",
        json={"scope": "workspace", "name": name, "description": "Use once."},
    )
    assert first.status_code == 200, first.text

    duplicate = integration_client.post(
        f"/api/skills?token={_TOKEN}",
        json={"scope": "workspace", "name": name, "description": "Use twice."},
    )

    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]


def test_file_save_rejects_invalid_skill_without_writing(integration_client, integration_workspace):
    skill_dir = integration_workspace / "skills" / f"save-guard-{uuid.uuid4().hex[:8]}"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    original = _skill_text(skill_dir.name, "Use before writing invalid skills.")
    skill_path.write_text(original, encoding="utf-8")

    rejected = integration_client.put(
        f"/api/files/content?token={_TOKEN}",
        json={"path": str(skill_path), "text": "---\nname: broken\n---\n# Broken\n"},
    )

    assert rejected.status_code == 422
    assert "description is required" in rejected.text
    assert skill_path.read_text(encoding="utf-8") == original

    ordinary = integration_workspace / f"ordinary-{uuid.uuid4().hex[:8]}.md"
    saved = integration_client.put(
        f"/api/files/content?token={_TOKEN}",
        json={"path": str(ordinary), "text": "# Ordinary\n", "create": True},
    )
    assert saved.status_code == 200, saved.text
    assert ordinary.read_text(encoding="utf-8") == "# Ordinary\n"


def test_skill_scope_auth_boundary_matches_shared_project_model(integration_client):
    project_id = _mk_project(integration_client, f"Skill Auth Project {uuid.uuid4().hex[:6]}")
    thread_id = _mk_session(integration_client, project_id=project_id)
    global_name = f"shared-global-{uuid.uuid4().hex[:8]}"
    project_name = f"shared-project-{uuid.uuid4().hex[:8]}"

    created = integration_client.post(
        f"/api/skills?token={_TOKEN}",
        json={
            "scope": "global",
            "name": global_name,
            "description": "Use across the shared workspace.",
        },
    )
    assert created.status_code == 200, created.text

    project_created = integration_client.post(
        f"/api/skills?token={_TOKEN}",
        json={
            "scope": "project",
            "project_id": project_id,
            "name": project_name,
            "description": "Use from the shared project.",
        },
    )
    assert project_created.status_code == 200, project_created.text

    shared_global = integration_client.get(f"/api/skills?token={_OTHER_TOKEN}&scope=global")
    assert shared_global.status_code == 200, shared_global.text
    assert any(row["name"] == global_name for row in shared_global.json()["skills"])

    shared_project = integration_client.get(
        f"/api/skills?token={_OTHER_TOKEN}&scope=project&project_id={project_id}"
    )
    assert shared_project.status_code == 200, shared_project.text
    assert any(row["name"] == project_name for row in shared_project.json()["skills"])

    derived_project = integration_client.get(
        f"/api/skills?token={_TOKEN}&scope=project&thread_id={thread_id}"
    )
    assert derived_project.status_code == 200, derived_project.text
    assert any(row["name"] == project_name for row in derived_project.json()["skills"])

    missing_project = integration_client.get(
        f"/api/skills?token={_TOKEN}&scope=project&project_id=missing-project"
    )
    assert missing_project.status_code == 404

    foreign_thread_write = integration_client.post(
        f"/api/skills?token={_OTHER_TOKEN}",
        json={
            "scope": "thread",
            "name": f"foreign-thread-{uuid.uuid4().hex[:8]}",
            "description": "Use from the wrong owner.",
            "thread_id": thread_id,
        },
    )
    assert foreign_thread_write.status_code == 404


def test_create_skill_rejects_system_scope(integration_client):
    """POST with scope=system is refused: system skills ship with CORSIA."""
    res = integration_client.post(
        f"/api/skills?token={_TOKEN}",
        json={"scope": "system", "name": "github", "description": "Nope."},
    )
    assert res.status_code == 422, res.text
    msg = res.json()["detail"][0]["msg"]
    assert "ship with CORSIA" in msg
    assert "CORSIA PR" in msg


def test_list_system_scope_uses_workspace_projection(integration_client):
    """GET scope=system lists agent-readable generated System skill paths."""
    res = integration_client.get(f"/api/skills?token={_TOKEN}&scope=system")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scope"] == "system"
    assert body["scope_id"] == "v1"
    assert body["root"].endswith(".corsia")
    names = {row["name"] for row in body["skills"]}
    assert "github" in names
    github = next(row for row in body["skills"] if row["name"] == "github")
    assert github["status"] == "included"
    assert github["scope"] == "system"
