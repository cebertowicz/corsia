"""Scheduled-update service-token behavior across the drain lifecycle."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException


def test_lifecycle_service_token_is_refused_by_session_only_check() -> None:
    """A lifecycle credential cannot make a human Settings decision."""
    from infra.auth import require_human_user

    with pytest.raises(HTTPException) as raised:
        require_human_user({"id": "scheduled", "token_kind": "service"})
    assert raised.value.status_code == 403


def test_drain_status_stays_scoped_to_the_authenticated_service_account(monkeypatch) -> None:
    """The shell's token uses the ordinary caller-scoped GET /drain route."""
    from api import drain as drain_api
    from infra import drain

    monkeypatch.setattr(
        drain_api,
        "authenticate",
        lambda token: {"id": "schedule-owner", "token_kind": "service"}
        if token == "service-token"
        else None,
    )
    seen: list[str] = []

    def caller_status(*, user_id: str) -> dict:
        seen.append(user_id)
        return {
            "active": True,
            "activeProcesses": 0,
            "blockingTurns": [{"threadId": "only-this-owner"}],
        }

    monkeypatch.setattr(drain, "get_drain_status", caller_status)
    status = asyncio.run(drain_api.get_drain_status_route("service-token"))

    assert seen == ["schedule-owner"]
    assert status["blockingTurns"] == [{"threadId": "only-this-owner"}]
    with pytest.raises(HTTPException) as raised:
        asyncio.run(drain_api.get_drain_status_route("other-user-token"))
    assert raised.value.status_code == 401


def test_drain_operation_status_projects_acquired_in_flight_operation(
    isolated_db, monkeypatch
) -> None:
    """The shell confirmation route returns the durable operation projection."""
    from api import drain as drain_api
    from automation import self_restart

    operation_id = self_restart.try_acquire(
        None, "schedule-owner", None, None, "update", kind="update"
    )
    assert operation_id is not None
    monkeypatch.setattr(
        drain_api,
        "authenticate",
        lambda _token: {"id": "schedule-owner", "token_kind": "service"},
    )

    status = asyncio.run(
        drain_api.get_drain_status_route("service-token", operation_id=operation_id)
    )

    assert status["operation_id"] == operation_id
    assert status["status"] == "in_flight"
    with pytest.raises(HTTPException) as raised:
        asyncio.run(drain_api.get_drain_status_route("service-token", operation_id="f" * 32))
    assert raised.value.status_code == 404
