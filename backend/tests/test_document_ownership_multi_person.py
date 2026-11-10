"""F3: the ownership payload reports the condition the ownership PUT refuses on.

The control must not offer the person as an owner when taking the document over
would be refused, so the load payload carries ``multi_person`` and the refusal
reads the same predicate (``document_supervisor.is_multi_person``). One test
covers both sides of each installation shape so the two cannot drift.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.auth import require_human_user

PERSON = {"id": "steve", "token_kind": "session"}
SECOND_PERSON = "leksi"


def _view(artifact_id: str):
    """The ownership payload as the document load builds it."""
    from infra import config
    from infra.database import get_db
    from registry.document_supervisor import ownership
    from registry import models

    db = get_db()
    try:
        return ownership(
            db, models.get_artifact(artifact_id, db=db),
            user_id=PERSON["id"], workspace=config.WORKSPACE,
        )
    finally:
        db.close()


def _one_person_account():
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("DELETE FROM auth_tokens WHERE user_id=?", (SECOND_PERSON,))
        db.execute("DELETE FROM users WHERE id=?", (SECOND_PERSON,))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def owned_document(isolated_db):
    """A registered document owned by an agent thread, on a two-person installation."""
    from infra import config
    from infra.database import get_db
    from registry import service

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,title,agent_enabled,created_at,updated_at) "
            "VALUES ('agent-1','steve','Documents & Artifacts',1,'t','t')",
        )
        db.commit()
        document = config.WORKSPACE / "shared" / "documents" / "reference.md"
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text("# Reference\n\nBody.\n", encoding="utf-8")
        artifact = service.register_write_strict(
            "agent-1", str(document),
            metadata={"update_trigger": "when the source facts change"}, db=db,
        )
        db.commit()
    finally:
        db.close()

    from api.registry_ownership import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_human_user] = lambda: PERSON
    return TestClient(app), artifact["id"]


def test_multi_person_installation_reports_the_flag_and_refuses_the_person(owned_document):
    from api.registry_ownership import MULTI_PERSON_REFUSAL

    client, artifact_id = owned_document

    assert _view(artifact_id)["multi_person"] is True
    refused = client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "user"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == MULTI_PERSON_REFUSAL


def test_single_person_installation_reports_no_flag_and_allows_the_person(owned_document):
    client, artifact_id = owned_document
    _one_person_account()

    assert _view(artifact_id)["multi_person"] is False
    changed = client.put(f"/api/artifacts/{artifact_id}/ownership", json={"owner": "user"})
    assert changed.status_code == 200, changed.text
    payload = changed.json()["ownership"]
    assert payload["multi_person"] is False
    assert payload["owner"]["kind"] == "person"
