"""Pending script decisions are bound to a person and reviewed hash."""
from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automation.script_schedules import create_script
from infra.auth import require_human_user, require_user


@pytest.mark.parametrize("decision,expected", [("approve", "approved"), ("reject", "denied")])
def test_script_decision_user_binding_and_single_use(isolated_db, tmp_path, monkeypatch, decision, expected):
    from api.host_access import router
    from events import delivery
    from infra import state

    db = sqlite3.connect(isolated_db)
    db.execute("INSERT INTO threads (id,user_id,created_at,updated_at) VALUES ('owner','person','t','t')")
    db.commit()
    db.close()
    path = tmp_path / "script.py"
    path.write_text("print(1)")
    auth = {"state": "pending", "application_hash": "a" * 64, "purpose": "work",
            "user_request": "please work", "trigger": {"cadence_seconds": None}, "review": {"reason": "appropriate"}}
    schedule = create_script("owner", "__workspace__", script_path=path, script_sha256="b" * 64,
                             cadence_seconds=None, series=[], timeout_seconds=5,
                             now="2026-01-01T00:00:00+00:00", db=None, authentication=auth)
    person = {"id": "person", "token_kind": "session"}
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: person
    app.dependency_overrides[require_human_user] = lambda: person
    monkeypatch.setattr(delivery, "deliver_to_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(state, "broadcast_to_user", lambda *args: None)
    client = TestClient(app)
    leases = client.get("/api/host-access/leases?root_thread_id=owner",
                        headers={"X-CORSIA-Interactive-UI": "1"}).json()["leases"]
    request = next(row for row in leases if row["kind"] == "script")
    assert request["script_path"] == str(path)
    assert request["reviewer_reason"] == "appropriate"
    assert request["state"] == "awaiting_user" and "revision" not in request
    payload = {"application_hash": request["application_hash"]}
    headers = {"X-CORSIA-User-Decision": request["decision_capability"]}
    person["id"] = "other"
    url = f"/api/host-access/leases/scripts/{schedule.id}/{decision}"
    assert client.post(url, json=payload, headers=headers).status_code == 403
    person["id"] = "person"
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 200 and response.json()["state"] == expected
    assert client.post(url, json=payload, headers=headers).status_code == 409
    delivery.deliver_to_thread.assert_awaited_once()
