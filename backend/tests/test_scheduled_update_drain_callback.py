"""Scheduled-update callback authorization across the drain lifecycle routes."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException


def test_callback_credential_only_controls_its_exact_inflight_drain_operation(monkeypatch) -> None:
    """The overnight shell can operate only its exact callback-owned lifecycle."""
    from api import drain as drain_api
    from automation import restart, self_restart
    from infra import db_async, drain

    operation_id = "a" * 32
    callback = "scheduled-callback"

    monkeypatch.setattr(drain_api, "authenticate", lambda _token: None)
    monkeypatch.setattr(
        self_restart,
        "callback_credential_matches",
        lambda candidate_id, candidate_token: candidate_id == operation_id and candidate_token == callback,
    )

    async def run_in_db(callback_fn, *args, **kwargs):
        return callback_fn(*args, **kwargs)

    monkeypatch.setattr(db_async, "run_in_db", run_in_db)
    monkeypatch.setattr(
        drain,
        "get_drain_operation_status",
        lambda candidate_id: {"operation_id": candidate_id, "active": True, "activeProcesses": 0},
    )
    monkeypatch.setattr(restart, "advance_restart_readiness", lambda candidate_id: candidate_id == operation_id)
    monkeypatch.setattr(restart, "mark_restarting", lambda candidate_id: candidate_id == operation_id)

    async def enforce(candidate_id):
        assert candidate_id == operation_id
        return {"force_stop_ready": True, "remaining": 0}

    async def abort(candidate_id, *, cancelled=False):
        return candidate_id == operation_id and cancelled

    monkeypatch.setattr(restart, "enforce_restart_hard_cap", enforce)
    monkeypatch.setattr(restart, "abort_restart_control", abort)

    status = asyncio.run(drain_api.get_drain_status_route(callback, operation_id))
    assert status["operation_id"] == operation_id
    assert asyncio.run(drain_api.drain_hard_cap(callback, {"operation_id": operation_id}))["ok"]
    assert asyncio.run(drain_api.drain_ready(callback, {"operation_id": operation_id}))["ok"]
    assert asyncio.run(drain_api.cancel_drain(callback, operation_id))["ok"]

    with pytest.raises(HTTPException, match="401"):
        asyncio.run(drain_api.get_drain_status_route(callback))
    with pytest.raises(HTTPException, match="401"):
        asyncio.run(drain_api.get_drain_status_route("wrong-callback", operation_id))
    with pytest.raises(HTTPException, match="401"):
        asyncio.run(drain_api.get_drain_status_route(callback, "b" * 32))


def test_callback_credential_is_refused_after_its_operation_settles(isolated_db) -> None:
    from automation import self_restart

    credential = self_restart.mint_callback_credential()
    operation_id = self_restart.try_acquire(
        None,
        None,
        None,
        None,
        "update",
        kind="update",
        callback_hash=self_restart.callback_credential_hash(credential),
    )
    assert operation_id is not None
    assert self_restart.callback_credential_matches(operation_id, credential)
    assert self_restart.abort_operation(operation_id, error="stopped", failure_code="build_failed")
    assert not self_restart.callback_credential_matches(operation_id, credential)


def test_outcome_seen_requires_a_human_session() -> None:
    from api import system_admin
    from infra.auth import require_human_user

    route = next(
        candidate for candidate in system_admin.router.routes
        if getattr(candidate, "path", None) == "/system/update-operations/{operation_id}/outcome-seen"
    )
    assert any(dependency.call is require_human_user for dependency in route.dependant.dependencies)
    with pytest.raises(HTTPException) as raised:
        require_human_user({"id": "machine", "token_kind": "api"})
    assert raised.value.status_code == 403
