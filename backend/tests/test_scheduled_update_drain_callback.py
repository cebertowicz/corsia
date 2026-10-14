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


def test_credential_expired_records_a_named_terminal_failure(monkeypatch) -> None:
    """An expired operation credential can only terminalize its own operation."""
    from api import drain as drain_api
    from automation import restart, self_restart
    from infra import accounts, db_async

    operation = {"id": "a" * 32, "requester_user": "schedule-owner"}

    async def run_in_db(callback, *args, **kwargs):
        return callback(*args, **kwargs)

    monkeypatch.setattr(db_async, "run_in_db", run_in_db)
    monkeypatch.setattr(self_restart, "operation_by_id", lambda operation_id: operation if operation_id == operation["id"] else None)
    monkeypatch.setattr(
        accounts,
        "expired_lifecycle_service_token_matches",
        lambda user_id, operation_id, token: (
            user_id == "schedule-owner" and operation_id == operation["id"] and token == "expired-token"
        ),
    )
    calls: list[tuple[str, str, str | None]] = []

    async def abort(operation_id: str, *, error: str, failure_code: str | None = None) -> bool:
        calls.append((operation_id, error, failure_code))
        return True

    monkeypatch.setattr(restart, "abort_restart_control", abort)
    result = asyncio.run(
        drain_api.credential_expired("expired-token", {"operation_id": operation["id"]})
    )

    assert result["failure_code"] == "credential_expired"
    assert calls == [(operation["id"], "lifecycle service credential expired", "credential_expired")]
