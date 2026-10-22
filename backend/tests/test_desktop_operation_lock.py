"""The desktop shell can query, but never inspect, the restart-operation lock."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

_REQUEST = object()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("in_flight", "expected"),
    [(None, {"active": False}), ({"id": "restart-1"}, {"active": True})],
)
async def test_operation_active_exposes_only_the_durable_lock(monkeypatch, in_flight, expected):
    from api import system
    from automation import self_restart

    async def fake_run_in_db(callback):
        assert callback is self_restart.in_flight_request
        return in_flight

    monkeypatch.setattr(system, "_trusted_request", lambda request: request is _REQUEST)
    monkeypatch.setattr(system, "run_in_db", fake_run_in_db)

    assert await system.system_operation_active(_REQUEST) == expected


@pytest.mark.asyncio
async def test_operation_active_refuses_an_untrusted_caller(monkeypatch):
    from api import system

    async def fail_run_in_db(callback):
        raise AssertionError("an untrusted caller must not reach the lock")

    monkeypatch.setattr(system, "_trusted_request", lambda request: False)
    monkeypatch.setattr(system, "run_in_db", fail_run_in_db)

    with pytest.raises(HTTPException) as refused:
        await system.system_operation_active(object())
    assert refused.value.status_code == 403


@pytest.mark.parametrize(
    ("host", "status"),
    # The first is the exact Host the desktop shell sends (lib.rs local_get_request).
    [("127.0.0.1:43123", 200), ("127.0.0.1", 403), ("127.0.0.1:43124", 403)],
)
def test_the_real_guard_admits_the_shells_host_and_nothing_portless(monkeypatch, host, status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import system

    async def fake_run_in_db(callback):
        return None

    monkeypatch.setattr(system, "run_in_db", fake_run_in_db)
    app = FastAPI()
    app.include_router(system.router)
    with TestClient(app, base_url="http://127.0.0.1:43123", client=("127.0.0.1", 50101)) as client:
        response = client.get("/api/system/operation-active", headers={"Host": host})
    assert response.status_code == status
    if status == 200:
        assert response.json() == {"active": False}


@pytest.mark.asyncio
async def test_stop_local_model_stops_only_what_the_supervisor_recorded(monkeypatch):
    from api import system
    from llm.providers import omlx_supervisor

    async def fake_stop_all():
        return [{"ok": True, "pid": 4242}]

    monkeypatch.setattr(system, "_trusted_json_post", lambda request: True)
    monkeypatch.setattr(omlx_supervisor, "stop_all_servers", fake_stop_all)

    assert await system.system_stop_local_model(object()) == {"stopped": [4242], }


@pytest.mark.asyncio
async def test_stop_local_model_refuses_an_untrusted_caller(monkeypatch):
    from api import system
    from llm.providers import omlx_supervisor

    async def fail_stop_all():
        raise AssertionError("an untrusted caller must not stop the model")

    monkeypatch.setattr(system, "_trusted_json_post", lambda request: False)
    monkeypatch.setattr(omlx_supervisor, "stop_all_servers", fail_stop_all)

    with pytest.raises(HTTPException) as refused:
        await system.system_stop_local_model(object())
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_stop_local_model_reports_a_failed_stop_as_an_error(monkeypatch):
    from api import system
    from llm.providers import omlx_supervisor

    async def fake_stop_all():
        return [{"ok": False, "message": "oMLX failed to stop (pid 4242)"}]

    monkeypatch.setattr(system, "_trusted_json_post", lambda request: True)
    monkeypatch.setattr(omlx_supervisor, "stop_all_servers", fake_stop_all)

    with pytest.raises(HTTPException) as failed:
        await system.system_stop_local_model(object())
    assert failed.value.status_code == 500


@pytest.mark.parametrize(("content_type", "status"), [("application/json", 200), ("text/plain", 403)])
def test_the_real_guard_admits_the_shells_stop_request_and_only_json(monkeypatch, content_type, status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import system
    from llm.providers import omlx_supervisor

    async def fake_stop_all():
        return []

    monkeypatch.setattr(omlx_supervisor, "stop_all_servers", fake_stop_all)
    app = FastAPI()
    app.include_router(system.router)
    with TestClient(app, base_url="http:" + "//127.0.0.1:43123", client=("127.0.0.1", 50101)) as client:
        response = client.post(
            "/api/system/stop-local-model", content="{}",
            headers={"Host": "127.0.0.1:43123", "Content-Type": content_type},
        )
    assert response.status_code == status
