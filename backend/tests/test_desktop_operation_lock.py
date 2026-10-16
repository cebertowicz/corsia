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
