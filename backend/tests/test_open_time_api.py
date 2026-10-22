"""POST /api/open-time: every visible run in one request, the user's own threads only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import open_time as open_time_api
from infra.auth import require_user
from tests.presence_helpers import USER, new_thread, pdb

__all__ = ["pdb"]


def _turn(db, thread: str, start: datetime, end: datetime | None) -> None:
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch) "
        "VALUES (?,?,0,'native/test',?,?,?,'boot')",
        (f"{thread}-{start.timestamp()}", thread, "completed" if end else "running",
         start.replace(tzinfo=None).isoformat(), end.replace(tzinfo=None).isoformat() if end else None),
    )


@pytest.fixture
def client(pdb):
    app = FastAPI()
    app.include_router(open_time_api.router, prefix="/api")
    app.dependency_overrides[require_user] = lambda: {"id": USER}
    with TestClient(app) as test_client:
        yield test_client


def test_one_request_answers_every_run_and_leaves_out_a_foreign_thread(pdb, client):
    lead = new_thread(pdb, "lead")
    new_thread(pdb, "worker", parent=lead)
    pdb.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES ('foreign','someone-else','native/test','t','t')"
    )
    start = datetime.now(UTC) - timedelta(hours=1)
    _turn(pdb, lead, start, start + timedelta(minutes=3))
    _turn(pdb, "worker", start + timedelta(minutes=2), None)
    pdb.commit()
    since = start.isoformat()

    body = {"items": [
        {"id": "run-1", "thread_id": lead, "since": since},
        {"id": "run-2", "thread_id": lead, "since": since, "until": (start + timedelta(minutes=1)).isoformat()},
        {"id": "run-3", "thread_id": "foreign", "since": since},
    ]}
    response = client.post("/api/open-time", json=body)

    assert response.status_code == 200
    items = response.json()["items"]
    assert set(items) == {"run-1", "run-2"}
    # The worker is still running, so the run is open and has counted since it started.
    assert items["run-1"]["open_now"] is True and items["run-1"]["seconds"] > 3600 - 120 - 5
    assert items["run-2"] == {"seconds": 60.0, "open_now": False, "as_of": items["run-2"]["as_of"]}
    assert items["run-2"]["as_of"].endswith("Z")


def test_a_duplicate_id_or_a_naive_time_is_refused(pdb, client):
    lead = new_thread(pdb, "lead")
    item = {"id": "run", "thread_id": lead, "since": datetime.now(UTC).isoformat()}
    assert client.post("/api/open-time", json={"items": [item, item]}).status_code == 422
    assert client.post("/api/open-time", json={"items": [{**item, "since": "2026-10-21T10:00:00"}]}).status_code == 422


def test_two_runs_of_one_thread_each_get_their_own_number_from_one_family_read(pdb, client, monkeypatch):
    from infra import open_time

    lead = new_thread(pdb, "lead")
    new_thread(pdb, "worker", parent=lead)
    start = datetime.now(UTC) - timedelta(hours=3)
    _turn(pdb, lead, start, start + timedelta(minutes=10))
    _turn(pdb, "worker", start + timedelta(minutes=5), start + timedelta(minutes=20))
    _turn(pdb, lead, start + timedelta(minutes=120), start + timedelta(minutes=130))
    pdb.commit()
    family_reads = []
    real = open_time.thread_and_descendants
    monkeypatch.setattr(
        open_time, "thread_and_descendants", lambda db, thread: family_reads.append(thread) or real(db, thread),
    )

    def at(minutes):
        return (start + timedelta(minutes=minutes)).isoformat()

    body = {"items": [
        {"id": "early", "thread_id": lead, "since": at(0), "until": at(30)},
        {"id": "late", "thread_id": lead, "since": at(110), "until": at(140)},
        {"id": "clipped", "thread_id": lead, "since": at(8), "until": at(125)},
    ]}
    items = client.post("/api/open-time", json=body).json()["items"]

    # The worker's overlap with the lead counts once: minutes 0 to 20.
    assert items["early"]["seconds"] == 20 * 60
    assert items["late"]["seconds"] == 10 * 60
    # Minutes 8 to 20 of the first run plus 120 to 125 of the second.
    assert items["clipped"]["seconds"] == (12 + 5) * 60
    assert family_reads == [lead]
