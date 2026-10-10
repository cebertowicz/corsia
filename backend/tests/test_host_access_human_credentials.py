"""Real token rows and HTTP decisions distinguish people from machine callers."""
from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from host_access.decision_capability import INTERACTIVE_UI_HEADER, USER_DECISION_HEADER


def _pending(isolated_db):
    from infra.host_access_leases import record_application_review, request_lease

    with sqlite3.connect(str(isolated_db)) as db:
        db.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("human-guard-root", "steve", "Guard fixture", "t", "t"),
        )
    lease = request_lease(
        "human-guard-root", purpose="Inspect inert fixture", plan=["Inspect"],
        duration_seconds=60,
    )
    return record_application_review(
        lease["id"], expected_revision=lease["revision"],
        application_hash=lease["application_hash"], allowed=True,
        reviewer_model="fixture/reviewer", review_result={"attestation": "Inert fixture."},
    )


def _client():
    from api.host_access import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("kind", ["api", "service"])
@pytest.mark.parametrize("detail", [False, True])
def test_machine_credentials_read_but_cannot_issue_human_capability(isolated_db, kind, detail):
    from infra.accounts import issue_token

    lease = _pending(isolated_db)
    credential, _ = issue_token("steve", kind=kind)
    url = "/api/host-access/leases" + (f"/{lease['id']}" if detail else "")
    with _client() as client:
        plain = client.get(url, headers={"Authorization": f"Bearer {credential}"})
        spoofed = client.get(url, headers={
            "Authorization": f"Bearer {credential}", INTERACTIVE_UI_HEADER: "1",
        })
    assert plain.status_code == 200
    row = plain.json() if detail else plain.json()["leases"][0]
    assert "decision_capability" not in row
    assert spoofed.status_code == 403
    assert spoofed.json()["detail"] == (
        "This change needs the signed-in person: open CORSIA and make it there."
    )


@pytest.mark.parametrize("kind", ["api", "service"])
@pytest.mark.parametrize("operation", ["approve", "reject", "revoke"])
def test_machine_credentials_cannot_reuse_same_user_human_capability(isolated_db, kind, operation):
    from infra.accounts import issue_token
    from infra.host_access_leases import get_lease

    lease = _pending(isolated_db)
    session, _ = issue_token("steve", kind="session")
    machine, _ = issue_token("steve", kind=kind)
    url = f"/api/host-access/leases/{lease['id']}"
    with _client() as client:
        viewed = client.get(url, headers={
            "Authorization": f"Bearer {session}", INTERACTIVE_UI_HEADER: "1",
        })
        assert viewed.status_code == 200
        capability = viewed.json()["decision_capability"]
        response = client.post(url + f"/{operation}", json={
            "revision": lease["revision"], "application_hash": lease["application_hash"],
        }, headers={"Authorization": f"Bearer {machine}", USER_DECISION_HEADER: capability})
    assert response.status_code == 403
    assert get_lease(lease["id"])["state"] == "awaiting_user"
    assert get_lease(lease["id"])["revision"] == lease["revision"]


def test_session_can_issue_and_use_human_rejection(isolated_db):
    from infra.accounts import issue_token
    from infra.host_access_leases import get_lease

    lease = _pending(isolated_db)
    session, _ = issue_token("steve", kind="session")
    url = f"/api/host-access/leases/{lease['id']}"
    with _client() as client:
        viewed = client.get(url, headers={
            "Authorization": f"Bearer {session}", INTERACTIVE_UI_HEADER: "1",
        })
        assert viewed.status_code == 200
        response = client.post(url + "/reject", json={
            "revision": lease["revision"], "application_hash": lease["application_hash"],
        }, headers={
            "Authorization": f"Bearer {session}",
            USER_DECISION_HEADER: viewed.json()["decision_capability"],
        })
    assert response.status_code == 200
    assert response.json()["state"] == "rejected"
    assert get_lease(lease["id"])["user_decision"] == "rejected"


def test_credential_kind_comes_from_canonical_lookup(isolated_db):
    from infra.accounts import issue_token, resolve_token
    from infra.auth import authenticate

    for kind in ("session", "api", "service"):
        credential, _ = issue_token("steve", kind=kind)
        assert resolve_token(credential)["token_kind"] == kind
        assert authenticate(credential)["token_kind"] == kind


@pytest.mark.parametrize("kind", [None, "agent", "unknown", "api", "service"])
def test_human_dependency_fails_closed_for_missing_or_nonhuman_kind(kind):
    from fastapi import HTTPException
    from infra.auth import require_human_user

    with pytest.raises(HTTPException) as failure:
        require_human_user({"id": "steve", "token_kind": kind})
    assert failure.value.status_code == 403


@pytest.mark.parametrize("change", ["revision", "hash", "wrong_owner", "expired", "missing_capability"])
def test_session_provenance_does_not_replace_current_decision_binding(isolated_db, change):
    from infra.accounts import issue_token, token_digest
    from infra.host_access_leases import get_lease

    lease = _pending(isolated_db)
    session, _ = issue_token("steve", kind="session")
    url = f"/api/host-access/leases/{lease['id']}"
    with _client() as client:
        viewed = client.get(url, headers={
            "Authorization": f"Bearer {session}", INTERACTIVE_UI_HEADER: "1",
        })
        body = {"revision": lease["revision"], "application_hash": lease["application_hash"]}
        capability = viewed.json()["decision_capability"]
        if change == "revision":
            body["revision"] += 1
        elif change == "hash":
            body["application_hash"] = "0" * 64
        elif change == "wrong_owner":
            session, _ = issue_token("leksi", kind="session")
        elif change == "expired":
            with sqlite3.connect(str(isolated_db)) as db:
                db.execute("UPDATE auth_tokens SET expires_at=? WHERE token_hash=?",
                           ("2000-01-01T00:00:00", token_digest(session)))
        elif change == "missing_capability":
            capability = ""
        response = client.post(url + "/reject", json=body, headers={
            "Authorization": f"Bearer {session}", USER_DECISION_HEADER: capability,
        })
    assert response.status_code == {"wrong_owner": 404, "expired": 401}.get(change, 403)
    assert get_lease(lease["id"])["state"] == "awaiting_user"
