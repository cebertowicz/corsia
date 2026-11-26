"""GET /api/system's CPU figure: psutil, in-process, never a subprocess.

Every open page polls this route, so a blocking CPU reading costs seconds on each
page load. These tests pin the cheap contract: no subprocess is started, the call
asks for the window since the previous poll (interval=None), and the response shape
and error branch are unchanged.
"""

from __future__ import annotations

import subprocess

import pytest
from auth_fixtures import OWNER_TOKEN
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def system_client(isolated_db, monkeypatch):
    """The system router with a cold status cache, so collection really runs."""
    from api import system

    monkeypatch.setitem(system._system_cache, "data", None)
    app = FastAPI()
    app.include_router(system.router)
    with TestClient(app) as client:
        yield client


def _system_body(client) -> dict:
    return client.get(
        "/api/system", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
    ).json()


def test_cpu_comes_from_psutil_with_no_subprocess(system_client, monkeypatch):
    import psutil

    started: list[tuple] = []

    def record_run(*args, **kwargs):
        started.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", record_run)
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 37.5)

    cpu = _system_body(system_client)["cpu"]

    assert started == []
    assert cpu == {"usage_pct": 37.5, "idle_pct": 62.5}
    assert 0.0 <= cpu["usage_pct"] <= 100.0
    assert 0.0 <= cpu["idle_pct"] <= 100.0


def test_cpu_reading_never_blocks_on_an_interval(system_client, monkeypatch):
    """interval=None is the non-blocking form; any number would stall the request."""
    import psutil

    asked: list[object] = []

    def fake(interval: object = 10.0) -> float:
        asked.append(interval)
        return 12.04

    monkeypatch.setattr(psutil, "cpu_percent", fake)

    cpu = _system_body(system_client)["cpu"]

    assert asked == [None]
    assert cpu == {"usage_pct": 12.0, "idle_pct": 88.0}


def test_cpu_fields_render_from_real_psutil(system_client):
    """Uncollected by the monkeypatched cases: the route really reads host CPU."""
    cpu = _system_body(system_client)["cpu"]

    assert "error" not in cpu
    assert 0.0 <= cpu["usage_pct"] <= 100.0
    assert 0.0 <= cpu["idle_pct"] <= 100.0


def test_cpu_error_branch_survives(system_client, monkeypatch):
    import psutil

    def unreadable(interval=None) -> float:
        raise RuntimeError("cpu unavailable")

    monkeypatch.setattr(psutil, "cpu_percent", unreadable)

    assert _system_body(system_client)["cpu"] == {"error": "cpu unavailable"}
