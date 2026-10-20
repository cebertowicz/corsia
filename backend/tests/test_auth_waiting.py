"""Concurrent sign-ins reserve a normalized name before the first DB await."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from api import auth
from auth_fixtures import TEST_PASSWORD


@pytest.fixture
def waiting_state(monkeypatch):
    monkeypatch.setattr(auth, "_waiting_names", set())
    monkeypatch.setattr(auth, "_waiting_attempts", 0)
    monkeypatch.setattr(auth, "_failures", {})
    monkeypatch.setattr(auth, "_login_lock", asyncio.Lock())


@pytest.mark.asyncio
@pytest.mark.parametrize("cap_full", [False, True])
async def test_parallel_same_name_has_exactly_one_waiter(waiting_state, monkeypatch, cap_full):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    initial = auth._MAX_WAITING_ATTEMPTS if cap_full else 0
    monkeypatch.setattr(auth, "_waiting_attempts", initial)
    monkeypatch.setattr(auth, "_HELD_SECONDS", 0)

    async def database(function, *args):
        calls.append(function)
        if function is auth.accounts.get_user:
            entered.set()
            await release.wait()
            return None
        assert function is auth.accounts.check_password
        return None

    monkeypatch.setattr(auth, "run_in_db", database)
    app = FastAPI()
    app.include_router(auth.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.post("/api/auth/login", json={"username": "nobody", "password": TEST_PASSWORD}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            duplicate = await asyncio.wait_for(client.post("/api/auth/login", json={"username": "NOBODY", "password": TEST_PASSWORD}), 1)
            assert duplicate.status_code == 429
            assert duplicate.headers["Retry-After"] == "1"
            assert calls == [auth.accounts.get_user]
            assert auth._waiting_names == {"nobody"}
        finally:
            release.set()
            refused = await first
    assert refused.status_code == 401
    assert auth._waiting_names == set()
    assert auth._waiting_attempts == initial
    assert calls.count(auth.accounts.check_password) == (0 if cap_full else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_name_reservation_is_released_if_account_lookup_fails(waiting_state, monkeypatch, cancel):
    entered, fail = asyncio.Event(), asyncio.Event()

    async def database(*args):
        entered.set()
        await fail.wait()
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(auth, "run_in_db", database)
    request = auth.LoginRequest(username="nobody", password=TEST_PASSWORD)
    attempt = asyncio.create_task(auth.login(request))
    await asyncio.wait_for(entered.wait(), 1)
    assert auth._waiting_names == {"nobody"}
    if cancel:
        attempt.cancel()
    else:
        fail.set()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await attempt
    assert auth._waiting_names == set()
    assert auth._waiting_attempts == 0
