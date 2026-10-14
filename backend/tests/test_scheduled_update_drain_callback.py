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
