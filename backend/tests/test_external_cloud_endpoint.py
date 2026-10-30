"""A cloud endpoint registered by URL: its models get real windows and a card list.

Generic ``/v1/models`` carries no context window, and the native harness refuses
a row without one, so every model on such an endpoint used to be unusable.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

_ALIBABA_HOST = "ws-example.us-east-1.maas.aliyuncs.com"
_ALIBABA_BASE = f"https://{_ALIBABA_HOST}/compatible-mode/v1"


@pytest.fixture
def model_table(monkeypatch):
    import litellm

    table = {
        "dashscope/qwen-flash": {"max_input_tokens": 997_952},
        "dashscope/glm-5.1": {"max_input_tokens": 202_745},
        # Another provider's number for the same name must never be borrowed.
        "openrouter/qwen-plus": {"max_input_tokens": 1_000_000},
    }
    monkeypatch.setattr(litellm, "model_cost", table)
    return table


def test_window_comes_from_the_hosts_own_provider_namespace(model_table) -> None:
    from llm.external_provider import reference_context_window

    assert reference_context_window(_ALIBABA_HOST, "qwen-flash") == 997_952
    assert reference_context_window(_ALIBABA_HOST.upper(), "glm-5.1") == 202_745


def test_no_window_is_borrowed_across_providers_or_hosts(model_table) -> None:
    from llm.external_provider import reference_context_window

    assert reference_context_window(_ALIBABA_HOST, "qwen-plus") is None
    assert reference_context_window("api.example.com", "qwen-flash") is None
    assert reference_context_window("notaliyuncs.com", "qwen-flash") is None
    assert reference_context_window("", "qwen-flash") is None


async def _discover(monkeypatch, *, meta: dict | None = None) -> str:
    from llm import external_provider as ep
    from llm import network_discovery as nd
    from llm import network_endpoints as ne

    sid = nd.upsert_server(
        provider="external", instance_id=_ALIBABA_HOST, instance_name="Alibaba",
        host=_ALIBABA_HOST, port=443, kind="external", dialect="openai",
        status="online",
    )
    # Registered by URL, as the add form stores it: discovery's stale check
    # reads the stored base the way dispatch does.
    nd.set_server_metadata(sid, base_url=_ALIBABA_BASE, **(meta or {}))
    monkeypatch.setattr(ne, "build_api_base", lambda _sid: _ALIBABA_BASE)

    async def _fetch(api_base, dialect, api_key):
        return dict.fromkeys(["qwen-flash", "glm-5.1", "qwen-flash-us"])

    monkeypatch.setattr(ep, "fetch_external_models", _fetch)
    result = await ep.discover_external_models(sid)
    assert result["ok"] is True
    return sid


def _windows(sid: str) -> dict[str, int | None]:
    from infra.database import get_db
    from llm.external_provider import served_model_id

    rows = get_db().execute(
        "SELECT litellm_model, context_window FROM models WHERE litellm_model LIKE ?",
        (f"external@{sid}/%",),
    ).fetchall()
    return {served_model_id(r["litellm_model"]): r["context_window"] for r in rows}


@pytest.mark.asyncio
async def test_discovery_records_the_published_window(isolated_db, model_table, monkeypatch):
    sid = await _discover(monkeypatch)

    assert _windows(sid) == {"qwen-flash": 997_952, "glm-5.1": 202_745, "qwen-flash-us": None}


@pytest.mark.asyncio
async def test_the_users_window_wins_over_the_published_one(isolated_db, model_table, monkeypatch):
    sid = await _discover(monkeypatch, meta={"context_window": 65_536})

    assert set(_windows(sid).values()) == {65_536}


@pytest.mark.asyncio
async def test_card_lists_the_catalog_rows_and_says_which_cannot_run(
    isolated_db, model_table, monkeypatch,
):
    from api.network_daemon_models import list_daemon_models
    from llm import network_discovery as nd

    sid = await _discover(monkeypatch)
    listed = await list_daemon_models(sid, nd.get_server(sid))

    assert listed["available"] == []
    by_id = {m["id"]: m for m in listed["loaded"]}
    assert set(by_id) == {"qwen-flash", "glm-5.1", "qwen-flash-us"}
    assert by_id["qwen-flash"]["context_length"] == 262_144
    # Nothing is saved, so the card shows the CORSIA default against the
    # server's own capacity rather than the raw published window.
    assert by_id["qwen-flash"]["provider_context_length"] == 997_952
    assert by_id["qwen-flash"]["context_window_source"] == "default"
    assert "unusable_reason" not in by_id["qwen-flash"]
    assert by_id["qwen-flash-us"]["unusable_reason"] == "context window unknown"


@pytest.mark.asyncio
async def test_card_counts_a_saved_checkpoint_window_like_dispatch_does(
    isolated_db, model_table, monkeypatch,
):
    from api.network_daemon_models import list_daemon_models
    from infra.database import get_db, init_model_profiles
    from llm import network_discovery as nd
    from llm.context_window import resolve_window_for_row

    sid = await _discover(monkeypatch)
    listed = await list_daemon_models(sid, nd.get_server(sid))
    db_ids = {m["id"]: m["db_id"] for m in listed["loaded"]}
    init_model_profiles()
    db = get_db()
    db.execute(
        "INSERT INTO model_profiles (model_id, context_window, updated_at) VALUES (?, ?, 't0')",
        (db_ids["qwen-flash-us"], 1_000_000),
    )
    db.commit()

    listed = await list_daemon_models(sid, nd.get_server(sid))
    by_id = {m["id"]: m for m in listed["loaded"]}
    row = db.execute("SELECT * FROM models WHERE id = ?", (db_ids["qwen-flash-us"],)).fetchone()
    db.close()

    saved = by_id["qwen-flash-us"]
    assert saved["context_length"] == 1_000_000 == resolve_window_for_row(row)
    assert saved["provider_context_length"] is None
    assert "unusable_reason" not in saved
    assert by_id["qwen-flash"]["provider_context_length"] == 997_952


@pytest.mark.asyncio
async def test_turning_a_model_off_withdraws_it_and_back_on_restores_it(
    isolated_db, model_table, monkeypatch,
):
    """The card's toggle is the catalog's own update, by the row id it lists."""
    from api import network_daemon_models as helpers
    from llm import catalog
    from llm import network_discovery as nd

    sid = await _discover(monkeypatch)
    listed = await helpers.list_daemon_models(sid, nd.get_server(sid))
    db_id = next(m["db_id"] for m in listed["loaded"] if m["id"] == "glm-5.1")

    assert catalog.update_model(db_id, {"enabled": 0}) is True
    listed = await helpers.list_daemon_models(sid, nd.get_server(sid))
    assert [m["id"] for m in listed["available"]] == ["glm-5.1"]

    catalog.update_model(db_id, {"enabled": 1})
    listed = await helpers.list_daemon_models(sid, nd.get_server(sid))
    assert listed["available"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["load_model", "unload_model"])
async def test_an_external_model_is_never_loaded_or_unloaded(
    isolated_db, model_table, monkeypatch, route,
):
    from api import network_servers

    sid = await _discover(monkeypatch)
    with pytest.raises(HTTPException) as err:
        await getattr(network_servers, route)(sid, network_servers.ModelBody(model="glm-5.1"))
    assert err.value.status_code == 400
