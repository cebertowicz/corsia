"""The desktop shell can query, but never inspect, the restart-operation lock."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("in_flight", "expected"),
    [(None, {"active": False}), ({"id": "restart-1"}, {"active": True})],
)
async def test_operation_active_exposes_only_the_durable_lock(monkeypatch, in_flight, expected):
    from api import system_admin
    from automation import self_restart
    from infra import db_async

    async def fake_run_in_db(callback):
        assert callback is self_restart.in_flight_request
        return in_flight

    monkeypatch.setattr(db_async, "run_in_db", fake_run_in_db)

    assert await system_admin.system_operation_active() == expected
