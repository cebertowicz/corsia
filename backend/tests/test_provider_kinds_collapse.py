"""Provider-kinds collapse: managed-engine seam, External provider, migration.

Covers the two-kind model (Managed oMLX + External) introduced when the bespoke
llama.cpp / LM Studio lifecycle was retired.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest


class _OfflineClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        return type("Response", (), {"status_code": 503})()


class _HeldStatusWrite:
    """A status write that holds its thread until the event loop answers it.

    Run off the loop, the loop answers at once. Run on the loop, nothing can
    answer: the write waits out its timeout and records that the loop stalled.
    No wall-clock threshold, so runner load cannot decide the result.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.answered = threading.Event()
        self.loop_ran_during_write = False

    def __call__(self, *_args, **_kwargs) -> None:
        self.started.set()
        self.loop_ran_during_write = self.answered.wait(5)

    async def answer(self) -> None:
        while not self.started.is_set():
            await asyncio.sleep(0.005)
        self.answered.set()


# --- managed-engine seam ---------------------------------------------------

def test_get_managed_engine_returns_omlx():
    from llm.managed_engine import OmlxEngine, get_managed_engine, is_managed

    assert is_managed("omlx") is True
    assert isinstance(get_managed_engine("omlx"), OmlxEngine)


def test_get_managed_engine_rejects_external():
    from llm.managed_engine import get_managed_engine, is_managed

    assert is_managed("external") is False
    with pytest.raises(ValueError):
        get_managed_engine("external")


def test_omlx_engine_satisfies_protocol():
    from llm.managed_engine import ManagedEngine, OmlxEngine

    assert isinstance(OmlxEngine(), ManagedEngine)  # runtime_checkable


# --- external-provider auth headers ---------------------------------------

def test_auth_headers_openai():
    from llm.external_provider import auth_headers

    assert auth_headers("openai", "sk-x") == {"Authorization": "Bearer sk-x"}
    assert auth_headers("openai", None) == {}


def test_auth_headers_anthropic():
    from llm.external_provider import auth_headers

    h = auth_headers("anthropic", "sk-ant")
    assert h["x-api-key"] == "sk-ant"
    assert "anthropic-version" in h
    # version header rides along even with no key; x-api-key only when present.
    assert "anthropic-version" in auth_headers("anthropic", None)
    assert "x-api-key" not in auth_headers("anthropic", None)


# --- external discovery ----------------------------------------------------

@pytest.mark.asyncio
async def test_discover_external_models_inserts_rows(isolated_db, monkeypatch):
    from infra.database import get_db
    from llm import external_provider as ep
    from llm import network_discovery as nd
    from llm import network_endpoints as ne

    sid = nd.upsert_server(
        provider="external", instance_id="127.0.0.1:9000",
        instance_name="LAN box", host="127.0.0.1", port=9000,
        kind="external", dialect="openai", status="online",
    )
    monkeypatch.setattr(ne, "build_api_base", lambda s: "http://127.0.0.1:9000/v1")

    async def _fake_fetch(api_base, dialect, api_key):
        return dict.fromkeys(["qwen3-8b", "llama-3-70b"])

    monkeypatch.setattr(ep, "fetch_external_models", _fake_fetch)

    res = await ep.discover_external_models(sid)
    assert res["ok"] is True
    assert res["discovered"] == 2

    db = get_db()
    rows = db.execute(
        "SELECT litellm_model, backend FROM models WHERE provider = 'external'"
    ).fetchall()
    db.close()
    lms = {r["litellm_model"] for r in rows}
    assert f"external@{sid}/qwen3-8b" in lms
    assert all(r["backend"] == "network" for r in rows)
    discovery = nd.get_server_metadata(sid)["discovery"]
    assert discovery["models"] == 2
    assert discovery["error"] is None
    assert discovery["at"]


@pytest.mark.asyncio
async def test_discover_external_models_offline_server_errors(isolated_db):
    from llm import external_provider as ep
    from llm import network_discovery as nd

    sid = nd.upsert_server(
        provider="external", instance_id="127.0.0.1:9001",
        instance_name="Down box", host="127.0.0.1", port=9001,
        kind="external", dialect="openai", status="offline",
    )
    res = await ep.discover_external_models(sid)
    assert res["ok"] is False
    assert "offline" in res["error"].lower()
    discovery = nd.get_server_metadata(sid)["discovery"]
    assert discovery["models"] == 0
    assert discovery["error"] == "Server is offline"
    assert discovery["at"]


@pytest.mark.asyncio
async def test_discover_external_models_records_missing_endpoint_early_exit(isolated_db):
    from llm import external_provider as ep
    from llm import network_discovery as nd

    sid = nd.upsert_server(
        provider="external", instance_id="missing.example.com/v1",
        instance_name="Missing base", host="missing.example.com", port=443,
        kind="external", dialect="openai", status="online",
    )
    res = await ep.discover_external_models(sid)

    assert res["ok"] is False
    assert res["error"] == "Server has no reachable endpoint"
    discovery = nd.get_server_metadata(sid)["discovery"]
    assert discovery["models"] == 0
    assert discovery["error"] == res["error"]


# --- managed reachability: one health word, not two ------------------------

@pytest.mark.asyncio
async def test_freshness_guard_accepts_the_health_word_omlx_actually_sends(
    isolated_db, monkeypatch,
) -> None:
    """A stale-but-live managed server comes back reachable, not offline.

    The guard exists for the gaps the 30s sweep misses (sleep/wake, VPN
    toggle). It probes the same URL the sweep probes, so it has to accept the
    same body: a real oMLX answers ``{"status": "ok"}``. Reading a different
    word here means the guard reports OFFLINE for a server that just replied
    normally, and the turn that triggered it fails on a working provider.
    """
    import httpx

    from llm import network_discovery as nd
    from llm import network_endpoints as ne

    sid = nd.upsert_server(
        provider="omlx", instance_id="127.0.0.1:5200", instance_name="oMLX",
        host="127.0.0.1", port=5200, kind="managed", status="online",
    )
    # Stale: no probe inside the freshness window, which is what makes the
    # guard do its work instead of short-circuiting on a fresh row.
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "UPDATE network_server_endpoints SET last_ok_at='2020-01-01T00:00:00+00:00'"
        " WHERE server_id=?", (sid,),
    )
    conn.commit()
    conn.close()
    assert ne.get_active_endpoint(sid) is None

    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        # Verbatim shape of a live oMLX 0.5.7 /api/status reply.
        return httpx.Response(200, json={
            "status": "ok", "version": "0.5.7", "models_loaded": 1,
        })

    transport = httpx.MockTransport(_handler)
    original = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(ne.httpx, "AsyncClient", _client)

    assert await ne.freshness_guard(sid) is True
    assert seen and seen[0].endswith("/api/status")
    assert ne.get_active_endpoint(sid) is not None
    assert ne.build_api_base(sid) == "http://127.0.0.1:5200/v1"
    from llm.network_discovery import get_server

    assert get_server(sid)["status"] == "online"


@pytest.mark.asyncio
async def test_freshness_status_write_never_blocks_the_event_loop(monkeypatch):
    from llm import network_discovery as nd
    from llm import network_endpoints as ne

    monkeypatch.setattr(ne, "get_active_endpoint", lambda _sid: None)
    monkeypatch.setattr(ne, "get_endpoints", lambda _sid: [{"host": "127.0.0.1"}])
    monkeypatch.setattr(ne, "_server_port", lambda _sid: 5200)
    monkeypatch.setattr(nd, "get_server", lambda _sid: {"kind": "managed"})
    monkeypatch.setattr(ne.httpx, "AsyncClient", lambda **_kwargs: _OfflineClient())

    write = _HeldStatusWrite()
    monkeypatch.setattr(nd, "update_server_status", write)

    _, reachable = await asyncio.wait_for(
        asyncio.gather(write.answer(), ne.freshness_guard("ns-blocked")), 10,
    )

    assert write.loop_ran_during_write
    assert reachable is False


@pytest.mark.asyncio
async def test_health_sweep_status_write_never_blocks_the_event_loop(monkeypatch):
    from llm import network_discovery as nd

    monkeypatch.setattr(nd, "get_endpoints", lambda _sid: [{"host": "127.0.0.1"}])
    monkeypatch.setattr(nd, "_mark_server_models_unavailable", lambda _sid, _provider, *, db: None)
    monkeypatch.setattr(nd, "_notify_models_changed_safe", lambda: None)

    write = _HeldStatusWrite()
    monkeypatch.setattr(nd, "update_server_status", write)
    server = {
        "id": "ns-blocked",
        "kind": "managed",
        "provider": "omlx",
        "port": 5200,
        "status": "online",
    }

    await asyncio.wait_for(
        asyncio.gather(write.answer(), nd._sweep_managed(_OfflineClient(), server)), 10,
    )

    assert write.loop_ran_during_write
