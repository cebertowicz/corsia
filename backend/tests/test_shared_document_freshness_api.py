from __future__ import annotations

import uuid

import pytest

from auth_fixtures import OWNER_TOKEN

pytestmark = pytest.mark.integration
_AUTH = "?token=" + OWNER_TOKEN


def _project_and_category(client, workspace):
    name = f"Freshness API {uuid.uuid4().hex[:8]}"
    response = client.post("/api/projects" + _AUTH, json={"name": name})
    assert response.status_code == 200, response.text
    project_id = response.json()["project"]["id"]
    root = workspace / "projects" / name.lower().replace(" ", "-")
    category = root / "shared" / "documents"
    category.mkdir(parents=True)
    (root / "shared" / "INDEX.md").write_text("# Shared\n", encoding="utf-8")
    (category / "INDEX.md").write_text("# Documents\n", encoding="utf-8")
    return project_id, root, category


def _thread(client, project_id):
    response = client.post(
        "/api/threads" + _AUTH,
        json={"title": "Documents & Artifacts", "project_id": project_id},
    )
    assert response.status_code == 200, response.text
    return response.json()["thread_id"]


def test_content_revision_tree_and_verify_endpoint_share_registry_freshness(
    integration_client,
    integration_workspace,
):
    from infra.database import get_db
    from registry import service

    project_id, _root, category = _project_and_category(
        integration_client, integration_workspace
    )
    owner = _thread(integration_client, project_id)
    path = category / "reference.md"
    path.write_text("# Reference\n\nBody.\n", encoding="utf-8")
    db = get_db()
    db.execute(
        "UPDATE threads SET agent_enabled=1,agent_responsibility='Own documents' "
        "WHERE id=?",
        (owner,),
    )
    db.commit()
    artifact = service.register_write_strict(
        owner,
        str(path),
        metadata={"update_trigger": "when source facts change"},
        db=db,
    )
    db.close()

    content = integration_client.get(
        "/api/files/content",
        params={
            "token": OWNER_TOKEN,
            "path": str(path),
            "project_id": project_id,
            "thread_id": owner,
        },
    )
    revision = integration_client.get(
        "/api/files/revision",
        params={
            "token": OWNER_TOKEN,
            "path": str(path),
            "project_id": project_id,
            "artifact_id": artifact["id"],
        },
    )
    tree = integration_client.get(
        "/api/files/tree",
        params={"token": OWNER_TOKEN, "project_id": project_id},
    )

    assert content.status_code == revision.status_code == tree.status_code == 200
    assert content.json()["freshness"]["state"] == "never_verified"
    assert content.json()["freshness"]["owner_name"] == "Documents & Artifacts"
    assert revision.json()["artifact"]["id"] == artifact["id"]
    assert revision.json()["freshness"]["state"] == "never_verified"
    nodes = [node for root in tree.json()["roots"] for node in root["nodes"]]
    tree_row = next(node for node in nodes if node["name"] == "reference.md")
    assert tree_row["owner_name"] == "Documents & Artifacts"
    assert tree_row["freshness"] == {"state": "never_verified"}

    verified = integration_client.post(
        f"/api/artifacts/{artifact['id']}/verify" + _AUTH
    )
    refreshed = integration_client.get(
        "/api/files/content",
        params={"token": OWNER_TOKEN, "path": str(path), "project_id": project_id},
    )
    assert verified.status_code == 200, verified.text
    assert refreshed.json()["freshness"]["state"] == "current"


def test_verify_endpoint_status_codes(
    integration_client,
    integration_workspace,
    monkeypatch,
):
    from infra.database import get_db
    from registry import service

    project_id, _root, category = _project_and_category(
        integration_client, integration_workspace
    )
    owner = _thread(integration_client, project_id)
    shared = category / "status.md"
    shared.write_text("# Status\n", encoding="utf-8")
    ordinary = integration_workspace / f"ordinary-{uuid.uuid4().hex[:8]}.md"
    ordinary.write_text("# Ordinary\n", encoding="utf-8")
    db = get_db()
    db.execute("UPDATE threads SET agent_enabled=1 WHERE id=?", (owner,))
    db.commit()
    shared_artifact = service.register_write_strict(owner, str(shared), db=db)
    ordinary_artifact = service.register_write_strict(owner, str(ordinary), db=db)
    db.close()

    unauthorized = integration_client.post(
        f"/api/artifacts/{shared_artifact['id']}/verify?token=invalid"
    )
    missing = integration_client.post(
        "/api/artifacts/not-an-artifact/verify" + _AUTH
    )
    non_shared = integration_client.post(
        f"/api/artifacts/{ordinary_artifact['id']}/verify" + _AUTH
    )
    assert unauthorized.status_code == 401
    assert missing.status_code == 404
    assert non_shared.status_code == 400

    def denied(*_args, **_kwargs):
        raise PermissionError("status.md is owned by Documents & Artifacts")

    monkeypatch.setattr("api.registry_pane.mark_verified", denied)
    forbidden = integration_client.post(
        f"/api/artifacts/{shared_artifact['id']}/verify" + _AUTH
    )
    assert forbidden.status_code == 403
    assert "owned by Documents & Artifacts" in forbidden.text


def test_tree_resolves_only_walked_artifact_paths(
    integration_client,
    integration_workspace,
    monkeypatch,
):
    from api import files_roots
    from registry import service

    project_id, _root, category = _project_and_category(
        integration_client, integration_workspace
    )
    _other_id, _other_root, other_category = _project_and_category(
        integration_client, integration_workspace
    )
    walked = category / "walked.md"
    elsewhere = other_category / "elsewhere.md"
    for path in (walked, elsewhere):
        path.write_text("# Doc\n", encoding="utf-8")
        service.register_write_strict(_thread(integration_client, project_id), str(path))
    checked: list[str] = []
    real = files_roots.is_under_shared_roots

    def spy(path, roots):
        checked.append(str(path))
        return real(path, roots)

    monkeypatch.setattr(files_roots, "is_under_shared_roots", spy)
    tree = integration_client.get(f"/api/files/tree{_AUTH}&project_id={project_id}")

    assert tree.status_code == 200, tree.text
    assert str(walked.resolve()) in checked
    assert str(elsewhere.resolve()) not in checked
