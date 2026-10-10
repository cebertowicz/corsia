from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
from auth_fixtures import OWNER_TOKEN, OTHER_TOKEN


def _seed(path) -> None:
    db = sqlite3.connect(str(path))
    db.executemany(
        "INSERT INTO threads (id,user_id,title,created_at,updated_at) "
        "VALUES (?,?,?,?,?)",
        [
            ("steve-root", "steve", "Steve root", "t", "t"),
            ("leksi-root", "leksi", "Leksi root", "t", "t"),
        ],
    )
    db.commit()
    db.close()


def _client():
    from api.host_access import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


_PUBLIC_TIMESTAMP_FIELDS = (
    "application_reviewed_at",
    "user_decided_at",
    "activated_at",
    "activation_wake_delivered_at",
    "expires_at",
    "created_at",
    "updated_at",
)


def _assert_public_timestamps_are_utc_z(row: dict) -> None:
    for field in _PUBLIC_TIMESTAMP_FIELDS:
        value = row[field]
        if value is None:
            continue
        assert value.endswith("Z"), (field, value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        assert parsed.utcoffset() == timedelta(0), (field, value)


def test_utc_storage_iso_to_api_normalizes_naive_and_offset_values():
    from infra.time import utc_storage_iso_to_api

    assert utc_storage_iso_to_api(None) is None
    assert utc_storage_iso_to_api("2026-07-21T06:56:25.010695") == (
        "2026-07-21T06:56:25.010695Z"
    )
    assert utc_storage_iso_to_api("2026-07-21T08:56:25.010695+02:00") == (
        "2026-07-21T06:56:25.010695Z"
    )


def test_authenticated_list_get_and_foreign_rows_are_hidden(isolated_db):
    from infra.host_access_leases import (
        activate_lease,
        approve_lease,
        get_lease,
        record_activation_wake_delivery,
        record_application_review,
        request_lease,
    )

    _seed(isolated_db)
    db = sqlite3.connect(str(isolated_db))
    db.row_factory = sqlite3.Row
    stored_at = datetime(2026, 7, 21, 6, 56, 25, 10695)
    steve = request_lease(
        "steve-root",
        purpose="Steve work",
        plan=["Inspect"],
        duration_seconds=1800,
        now=stored_at,
        db=db,
    )
    reviewed = record_application_review(
        steve["id"],
        expected_revision=steve["revision"],
        application_hash=steve["application_hash"],
        allowed=True,
        reviewer_model="native/reviewer",
        review_result={"attestation": "Bounded."},
        now=stored_at,
        db=db,
    )
    approved = approve_lease(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
        now=stored_at,
        db=db,
    )
    active = activate_lease(
        approved["id"],
        expected_revision=approved["revision"],
        application_hash=approved["application_hash"],
        now=stored_at,
        db=db,
    )
    record_activation_wake_delivery(
        active["id"],
        expected_revision=active["revision"],
        application_hash=active["application_hash"],
        now=stored_at,
        db=db,
    )
    leksi = request_lease(
        "leksi-root",
        purpose="Leksi work",
        plan=["Inspect"],
        duration_seconds=60,
        db=db,
    )
    assert not get_lease(active["id"], db=db)["expires_at"].endswith("Z")
    db.close()

    with _client() as client:
        unauthorized = client.get("/api/host-access/leases")
        listed = client.get(
            "/api/host-access/leases",
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )
        own = client.get(
            f"/api/host-access/leases/{steve['id']}?token={OWNER_TOKEN}"
        )
        foreign = client.get(
            f"/api/host-access/leases/{leksi['id']}?token={OWNER_TOKEN}"
        )

    assert unauthorized.status_code == 401
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["leases"]] == [steve["id"]]
    listed_lease = listed.json()["leases"][0]
    assert listed_lease["root_thread_id"] == "steve-root"
    assert listed_lease["root_thread_title"] == "Steve root"
    assert "root_session_id" not in listed_lease
    assert "root_session_title" not in listed_lease
    assert own.status_code == 200
    assert own.json()["root_thread_id"] == "steve-root"
    assert own.json()["root_thread_title"] == "Steve root"
    assert "root_session_id" not in own.json()
    assert "root_session_title" not in own.json()
    assert own.json()["requesting_project_id"] is None
    assert own.json()["repo_policy_snapshot"] == []
    assert len(own.json()["repo_policy_hash"]) == 64
    assert own.json()["requested_policy_exceptions"] == []
    assert own.json()["created_at"] == "2026-07-21T06:56:25.010695Z"
    _assert_public_timestamps_are_utc_z(listed.json()["leases"][0])
    _assert_public_timestamps_are_utc_z(own.json())
    assert foreign.status_code == 404


def test_list_query_filter_is_opt_in_and_default_body_is_unchanged(isolated_db):
    _seed(isolated_db)
    db = sqlite3.connect(str(isolated_db))
    db.executemany(
        """INSERT INTO host_access_leases (
            id,root_thread_id,user_id,state,purpose,plan,
            requested_duration_seconds,application_hash,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                "terminal-lease",
                "steve-root",
                "steve",
                "rejected",
                "Rejected lease",
                '["Inspect"]',
                30,
                "a" * 64,
                "2026-07-21T06:56:25.000000",
                "2026-07-21T06:56:25.000000",
            ),
            (
                "open-lease",
                "steve-root",
                "steve",
                "reviewing",
                "Open lease",
                '["Inspect"]',
                30,
                "b" * 64,
                "2026-07-21T06:56:26.000000",
                "2026-07-21T06:56:26.000000",
            ),
        ],
    )
    db.commit()
    db.close()

    with _client() as client:
        default = client.get(
            "/api/host-access/leases",
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )
        scoped = client.get(
            "/api/host-access/leases?root_thread_id=steve-root&nonterminal_only=true",
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )

    assert default.status_code == 200
    assert default.content == (
        b'{"leases":[{"id":"open-lease","root_thread_id":"steve-root",'
        b'"user_id":"steve","state":"reviewing","purpose":"Open lease",'
        b'"plan":["Inspect"],"requested_duration_seconds":30,'
        b'"expected_resources":null,"persistent_effects":null,'
        b'"rollback_plan":null,"forbidden_effects":null,'
        b'"requesting_project_id":null,"repo_policy_snapshot":[],'
        b'"repo_policy_hash":"ae3e2aca0c674a9150813698f7e5f14cd135a0dcd9324ead8044b85cd95ec70e",'
        b'"requested_policy_exceptions":[],"application_hash":"'
        + b"b" * 64
        + b'","revision":1,"reviewer_model":null,"application_review_result":null,'
        b'"application_reviewed_at":null,"user_decision":null,'
        b'"user_decided_at":null,"activated_at":null,'
        b'"activation_wake_delivered_at":null,"expires_at":null,'
        b'"terminal_reason":null,"created_at":"2026-07-21T06:56:26Z",'
        b'"updated_at":"2026-07-21T06:56:26Z",'
        b'"root_thread_title":"Steve root","reviewer":null},'
        b'{"id":"terminal-lease","root_thread_id":"steve-root",'
        b'"user_id":"steve","state":"rejected","purpose":"Rejected lease",'
        b'"plan":["Inspect"],"requested_duration_seconds":30,'
        b'"expected_resources":null,"persistent_effects":null,'
        b'"rollback_plan":null,"forbidden_effects":null,'
        b'"requesting_project_id":null,"repo_policy_snapshot":[],'
        b'"repo_policy_hash":"ae3e2aca0c674a9150813698f7e5f14cd135a0dcd9324ead8044b85cd95ec70e",'
        b'"requested_policy_exceptions":[],"application_hash":"'
        + b"a" * 64
        + b'","revision":1,"reviewer_model":null,"application_review_result":null,'
        b'"application_reviewed_at":null,"user_decision":null,'
        b'"user_decided_at":null,"activated_at":null,'
        b'"activation_wake_delivered_at":null,"expires_at":null,'
        b'"terminal_reason":null,"created_at":"2026-07-21T06:56:25Z",'
        b'"updated_at":"2026-07-21T06:56:25Z",'
        b'"root_thread_title":"Steve root","reviewer":null}]}'
    )
    assert scoped.status_code == 200
    assert [row["id"] for row in scoped.json()["leases"]] == ["open-lease"]


def test_user_decisions_require_non_model_visible_capability(
    isolated_db,
    monkeypatch,
):
    from api import host_access as host_access_api
    from infra.accounts import issue_token
    from infra.host_access_leases import (
        approve_lease,
        record_application_review,
        reject_lease,
        request_lease,
        revoke_lease,
    )

    owner_session, _ = issue_token("steve", kind="session")
    other_session, _ = issue_token("leksi", kind="session")

    async def call(operation, lease_id, **kwargs):
        return operation(lease_id, **kwargs)

    monkeypatch.setattr(
        host_access_api,
        "approve_and_activate",
        lambda lease_id, **kwargs: call(approve_lease, lease_id, **kwargs),
    )
    monkeypatch.setattr(
        host_access_api,
        "reject_pending_lease",
        lambda lease_id, **kwargs: call(reject_lease, lease_id, **kwargs),
    )
    monkeypatch.setattr(
        host_access_api,
        "revoke_active_lease",
        lambda lease_id, **kwargs: call(revoke_lease, lease_id, **kwargs),
    )

    _seed(isolated_db)
    db = sqlite3.connect(str(isolated_db))
    db.row_factory = sqlite3.Row
    requested = request_lease(
        "steve-root",
        purpose="Steve work",
        plan=["Inspect"],
        duration_seconds=60,
        db=db,
    )
    reviewed = record_application_review(
        requested["id"],
        expected_revision=requested["revision"],
        application_hash=requested["application_hash"],
        allowed=True,
        reviewer_model="native/reviewer",
        review_result={"attestation": "Bounded."},
        db=db,
    )
    db.close()

    body = {
        "revision": reviewed["revision"],
        "application_hash": reviewed["application_hash"],
    }
    with _client() as client:
        model_visible_list = client.get(
            "/api/host-access/leases",
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )
        interactive_list = client.get(
            "/api/host-access/leases",
            headers={
                "Authorization": f"Bearer {owner_session}",
                "X-CORSIA-Interactive-UI": "1",
            },
        )
        capability = interactive_list.json()["leases"][0]["decision_capability"]
        missing_capability = client.post(
            f"/api/host-access/leases/{reviewed['id']}/approve",
            headers={"Authorization": f"Bearer {owner_session}"},
            json=body,
        )
        approved = client.post(
            f"/api/host-access/leases/{reviewed['id']}/approve",
            headers={
                "Authorization": f"Bearer {owner_session}",
                "X-CORSIA-User-Decision": capability,
            },
            json=body,
        )
        stale_reject = client.post(
            f"/api/host-access/leases/{reviewed['id']}/reject",
            headers={
                "Authorization": f"Bearer {owner_session}",
                "X-CORSIA-User-Decision": capability,
            },
            json=body,
        )
        foreign = client.post(
            f"/api/host-access/leases/{reviewed['id']}/revoke",
            headers={
                "Authorization": f"Bearer {other_session}",
                "X-CORSIA-User-Decision": capability,
            },
            json={
                "revision": approved.json()["revision"],
                "application_hash": reviewed["application_hash"],
            },
        )

    assert model_visible_list.status_code == 200
    assert "decision_capability" not in model_visible_list.json()["leases"][0]
    assert interactive_list.status_code == 200
    assert capability
    assert missing_capability.status_code == 403
    assert approved.status_code == 200
    assert approved.json()["state"] == "activating"
    _assert_public_timestamps_are_utc_z(approved.json())
    assert stale_reject.status_code == 409
    assert foreign.status_code == 404


def test_api_delegates_live_list_get_and_revoke_to_owner_authority(
    isolated_db,
    monkeypatch,
):
    from api import host_access as host_access_api
    from infra.accounts import issue_token
    from infra.host_access_leases import (
        activate_lease,
        approve_lease,
        record_application_review,
        request_lease,
    )

    owner_session, _ = issue_token("steve", kind="session")
    _seed(isolated_db)
    requested = request_lease(
        "steve-root",
        purpose="Steve work",
        plan=["Inspect"],
        duration_seconds=60,
    )
    reviewed = record_application_review(
        requested["id"],
        expected_revision=requested["revision"],
        application_hash=requested["application_hash"],
        allowed=True,
        reviewer_model="native/reviewer",
        review_result={"attestation": "Bounded."},
    )
    approved = approve_lease(
        reviewed["id"],
        user_id="steve",
        expected_revision=reviewed["revision"],
        application_hash=reviewed["application_hash"],
    )
    active = activate_lease(
        approved["id"],
        expected_revision=approved["revision"],
        application_hash=approved["application_hash"],
    )
    calls: list[tuple[str, str]] = []

    async def owner_list(user_id, **_filters):
        calls.append(("list", user_id))
        return [active] if user_id == "steve" else []

    async def owner_get(lease_id, *, user_id):
        calls.append(("get", user_id))
        if user_id != "steve" or lease_id != active["id"]:
            from infra.host_access_leases import LeaseNotFoundError

            raise LeaseNotFoundError("not found")
        return active

    async def owner_revoke(lease_id, *, user_id, **_kwargs):
        calls.append(("revoke", user_id))
        assert lease_id == active["id"]
        return {
            **active,
            "state": "revoked",
            "revision": active["revision"] + 1,
            "terminal_reason": "user_revoked",
        }

    monkeypatch.setattr(host_access_api, "list_host_access_leases", owner_list)
    monkeypatch.setattr(host_access_api, "get_host_access_lease", owner_get)
    monkeypatch.setattr(host_access_api, "revoke_active_lease", owner_revoke)

    with _client() as client:
        interactive = client.get(
            "/api/host-access/leases",
            headers={
                "Authorization": f"Bearer {owner_session}",
                "X-CORSIA-Interactive-UI": "1",
            },
        )
        capability = interactive.json()["leases"][0]["decision_capability"]
        own = client.get(
            f"/api/host-access/leases/{active['id']}?token={OWNER_TOKEN}"
        )
        foreign = client.get(
            f"/api/host-access/leases/{active['id']}?token={OTHER_TOKEN}"
        )
        revoked = client.post(
            f"/api/host-access/leases/{active['id']}/revoke",
            headers={
                "Authorization": f"Bearer {owner_session}",
                "X-CORSIA-User-Decision": capability,
            },
            json={
                "revision": active["revision"],
                "application_hash": active["application_hash"],
            },
        )

    assert own.status_code == 200
    assert foreign.status_code == 404
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"
    assert calls == [
        ("list", "steve"),
        ("get", "steve"),
        ("get", "leksi"),
        ("get", "steve"),
        ("revoke", "steve"),
    ]
