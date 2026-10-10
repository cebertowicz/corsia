from __future__ import annotations

import uuid

import pytest

from auth_fixtures import OWNER_TOKEN


pytestmark = pytest.mark.integration
_AUTH = "?token=" + OWNER_TOKEN


def _project_and_category(client, workspace):
    name = f"Shared Owner Editor {uuid.uuid4().hex[:8]}"
    response = client.post(
        "/api/projects" + _AUTH,
        json={"name": name},
    )
    assert response.status_code == 200, response.text
    project_id = response.json()["project"]["id"]
    project_root = workspace / "projects" / name.lower().replace(" ", "-")
    shared = project_root / "shared"
    category = shared / "documents"
    category.mkdir(parents=True)
    (shared / "INDEX.md").write_text("# Shared\n", encoding="utf-8")
    (category / "INDEX.md").write_text("# Documents\n", encoding="utf-8")
    return project_id, category


def _create_thread(client, project_id: str) -> str:
    response = client.post(
        "/api/threads" + _AUTH,
        json={"title": "Document owner", "project_id": project_id},
    )
    assert response.status_code == 200, response.text
    return response.json()["thread_id"]


def test_editor_creation_needs_no_trigger_and_infers_only_dated_paths(
    integration_client,
    integration_workspace,
):
    from infra.database import get_db
    from registry import service
    from registry.identity_metadata import parse_metadata
    from registry.shared_principals import UI_PRINCIPAL, artifact_owner

    project_id, category = _project_and_category(
        integration_client, integration_workspace
    )
    thread_id = _create_thread(integration_client, project_id)
    db = get_db()
    db.execute(
        "UPDATE threads SET agent_enabled=1,agent_responsibility='Own documents' "
        "WHERE id=?",
        (thread_id,),
    )
    db.commit()

    ordinary = category / "editor-created.md"
    saved = integration_client.put(
        "/api/files/content" + _AUTH,
        json={
            "path": str(ordinary),
            "text": "ordinary\n",
            "description": "Editor-created shared document",
            "project_id": project_id,
            "thread_id": thread_id,
            "create": True,
        },
    )
    assert saved.status_code == 200, saved.text
    ordinary_artifact = service.find_file_identity(str(ordinary), db=db)
    assert ordinary_artifact is not None
    assert artifact_owner(ordinary_artifact["id"], db=db) == thread_id
    assert "update_trigger" not in parse_metadata(ordinary_artifact["metadata"])

    dated = category / "2026-09-29-editor-created.md"
    dated_saved = integration_client.put(
        "/api/files/content" + _AUTH,
        json={
            "path": str(dated),
            "text": "dated\n",
            "description": "Dated editor-created shared document",
            "project_id": project_id,
            "thread_id": thread_id,
            "create": True,
        },
    )
    assert dated_saved.status_code == 200, dated_saved.text
    dated_artifact = service.find_file_identity(str(dated), db=db)
    assert dated_artifact is not None
    assert parse_metadata(dated_artifact["metadata"])["update_trigger"] == (
        "none: dated record"
    )

    fallback = category / "editor-user-owned.md"
    fallback_saved = integration_client.put(
        "/api/files/content" + _AUTH,
        json={
            "path": str(fallback),
            "text": "user owned\n",
            "description": "User-owned editor document",
            "project_id": project_id,
            "create": True,
        },
    )
    assert fallback_saved.status_code == 200, fallback_saved.text
    fallback_artifact = service.find_file_identity(str(fallback), db=db)
    assert fallback_artifact is not None
    assert artifact_owner(fallback_artifact["id"], db=db) == UI_PRINCIPAL
