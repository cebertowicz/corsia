"""Endpoints that serve models they never list, and removal that stays removed.

Alibaba's Token Plan answers ``/models`` with an empty list and its Coding Plan
with 404, so their models exist only as ids the user declares.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing

import httpx
import pytest
from fastapi import HTTPException

from llm.external_declared_models import clean_model_ids, discoverable_model_ids
from omlx_provider_fixtures import _patch_httpx, _resp, _Router

_BASE = "https://token-plan.example.com/compatible-mode/v1"
_LIST = "/compatible-mode/v1/models"
_CHAT = ("POST", "/compatible-mode/v1/chat/completions")


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"{_BASE}/models")
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))


# ── the declared ids themselves ──────────────────────────────────────────────

def test_declared_ids_are_trimmed_split_free_and_deduplicated() -> None:
    assert clean_model_ids([" glm-5.3 ", "deepseek-v4.1-flash", "glm-5.3", ""]) == [
        "glm-5.3", "deepseek-v4.1-flash",
    ]
    assert clean_model_ids(None) is None
    assert clean_model_ids([]) == []


@pytest.mark.parametrize("bad", ["two words", "sk-sp-abc def", "a" * 201, "-leading"])
def test_a_value_that_is_not_a_model_id_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        clean_model_ids([bad])


def test_too_many_declared_ids_are_refused() -> None:
    with pytest.raises(ValueError):
        clean_model_ids([f"m{i}" for i in range(101)])


# ── discovery catalogs declared ids ──────────────────────────────────────────

def _discover(monkeypatch, listed, declared):
    from llm import external_provider as ep

    async def fetch(*_a):
        if isinstance(listed, Exception):
            raise listed
        return listed

    monkeypatch.setattr(ep, "fetch_external_models", fetch)
    return asyncio.run(discoverable_model_ids(_BASE, "openai", None, declared))


def test_an_empty_list_yields_the_declared_ids(monkeypatch) -> None:
    assert _discover(monkeypatch, [], ["glm-5.3"]) == ["glm-5.3"]


def test_a_missing_list_call_yields_the_declared_ids(monkeypatch) -> None:
    assert _discover(monkeypatch, _status_error(404), ["glm-5.3"]) == ["glm-5.3"]


def test_listed_ids_come_first_and_declared_ones_are_added(monkeypatch) -> None:
    assert _discover(monkeypatch, ["a"], ["b", "a"]) == ["a", "b"]


@pytest.mark.parametrize("code,declared", [(404, []), (401, ["glm-5.3"]), (500, ["glm-5.3"])])
def test_other_list_failures_still_fail(monkeypatch, code: int, declared: list[str]) -> None:
    with pytest.raises(httpx.HTTPStatusError):
        _discover(monkeypatch, _status_error(code), declared)


def test_discovery_reconciles_the_declared_ids_of_an_unlisted_endpoint(monkeypatch) -> None:
    from llm import external_provider as ep

    server = {"id": "ns-tp", "status": "online", "dialect": "openai"}
    reconciled: list = []

    async def fetch(*_a):
        raise _status_error(404)

    def reconcile(sid, srv, dialect, model_ids, caps, *rest):
        reconciled.append(model_ids)
        return {"ok": True}

    monkeypatch.setattr(ep, "_external_discovery_config",
                        lambda sid: (server, _BASE, None, {"declared_models": ["glm-5.3"]}))
    monkeypatch.setattr(ep, "fetch_external_models", fetch)
    monkeypatch.setattr(ep, "_reconcile_external_models", reconcile)
    assert asyncio.run(ep.discover_external_models("ns-tp")) == {"ok": True}
    assert reconciled == [["glm-5.3"]]


# ── the health sweep ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("reply,online", [
    (_resp(404), True),
    (_resp(200, {"data": []}), True),
    (_resp(401, {"code": "InvalidApiKey"}), False),
])
def test_the_sweep_keeps_a_declared_endpoint_online_and_its_models_ready(
    monkeypatch, reply, online: bool,
) -> None:
    from llm import network_discovery as nd

    server = {"id": "ns-tp", "kind": "external", "dialect": "openai", "port": 443,
              "status": "online", "provider": "external"}
    meta = {"base_url": _BASE, "declared_models": ["glm-5.3"]}
    offline: list = []
    monkeypatch.setattr(nd, "read_server_metadata", lambda sid: meta)
    monkeypatch.setattr(nd, "get_endpoints", lambda sid: [{"host": "token-plan.example.com"}])
    monkeypatch.setattr(nd, "upsert_endpoint", lambda *a, **k: None)
    monkeypatch.setattr(nd, "update_server_status", lambda *a, **k: None)
    monkeypatch.setattr(nd, "_record_server_offline", lambda srv, provider: offline.append(srv["id"]))
    monkeypatch.setattr(nd, "_notify_models_changed_safe", lambda: None)
    monkeypatch.setattr(nd, "_LOADED_BY_SERVER", {})
    router = _Router({("GET", _LIST): reply})

    async def _run() -> None:
        _patch_httpx(monkeypatch, router)
        async with httpx.AsyncClient() as client:
            await nd._sweep_external(client, server)

    asyncio.run(_run())
    assert offline == ([] if online else ["ns-tp"])
    assert nd.get_loaded_model_ids("ns-tp") == ({"glm-5.3"} if online else set())


# ── add and edit accept declared ids ─────────────────────────────────────────

def _server_meta(path, sid: str) -> dict:
    with closing(sqlite3.connect(str(path))) as db:
        raw = db.execute("SELECT metadata_json FROM network_servers WHERE id=?", (sid,)).fetchone()[0]
    return json.loads(raw or "{}")


def _quiet(monkeypatch) -> None:
    from infra import product_events
    from llm import network_discovery as nd

    monkeypatch.setattr(nd, "discover_and_notify", lambda sid: asyncio.sleep(0))
    monkeypatch.setattr(product_events, "emit_product_event", lambda *a, **k: None)


def test_an_endpoint_without_a_list_call_registers_with_declared_ids(
    isolated_db, monkeypatch,
) -> None:
    from api.network_server_helpers import add_external_server
    from api.network_servers import AddServerBody

    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({_CHAT: _resp(200)}))
    body = AddServerBody(name="TP", base_url=_BASE, dialect="openai",
                         model_ids=["glm-5.3", "deepseek-v4.1-flash"])
    out = asyncio.run(add_external_server(body))
    assert out["ok"] is True
    assert _server_meta(isolated_db, out["server_id"])["declared_models"] == [
        "glm-5.3", "deepseek-v4.1-flash",
    ]


def test_without_declared_ids_a_missing_list_call_still_refuses(isolated_db, monkeypatch) -> None:
    from api.network_server_helpers import add_external_server
    from api.network_servers import AddServerBody

    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({}))
    with pytest.raises(HTTPException) as err:
        asyncio.run(add_external_server(AddServerBody(name="TP", base_url=_BASE, dialect="openai")))
    assert "HTTP 404" in err.value.detail


def test_edit_keeps_omitted_ids_and_clears_an_empty_list(isolated_db, monkeypatch) -> None:
    from api.network_server_helpers import add_external_server, update_external_server
    from api.network_servers import AddServerBody, UpdateExternalServerBody

    _quiet(monkeypatch)
    router = _Router({_CHAT: _resp(200)})
    _patch_httpx(monkeypatch, router)
    sid = asyncio.run(add_external_server(
        AddServerBody(name="TP", base_url=_BASE, dialect="openai", model_ids=["glm-5.3"]),
    ))["server_id"]

    # Omitted: the stored ids stay, and they still excuse the missing list call.
    asyncio.run(update_external_server(sid, UpdateExternalServerBody(base_url=_BASE)))
    assert _server_meta(isolated_db, sid)["declared_models"] == ["glm-5.3"]

    # Cleared ids need a list again, so the endpoint now answers one.
    router.routes[("GET", _LIST)] = _resp(200, {"data": [{"id": "x"}]})
    asyncio.run(update_external_server(sid, UpdateExternalServerBody(base_url=_BASE, model_ids=[])))
    assert "declared_models" not in _server_meta(isolated_db, sid)


def test_edit_refuses_a_value_that_is_not_a_model_id(isolated_db, monkeypatch) -> None:
    from api.network_server_helpers import add_external_server, update_external_server
    from api.network_servers import AddServerBody, UpdateExternalServerBody

    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({_CHAT: _resp(200)}))
    sid = asyncio.run(add_external_server(
        AddServerBody(name="TP", base_url=_BASE, dialect="openai", model_ids=["glm-5.3"]),
    ))["server_id"]
    with pytest.raises(HTTPException) as err:
        asyncio.run(update_external_server(
            sid, UpdateExternalServerBody(base_url=_BASE, model_ids=["not an id"]),
        ))
    assert err.value.status_code == 400


def test_agent_use_restriction_round_trips_through_endpoint_metadata(isolated_db, monkeypatch) -> None:
    from api.network_server_helpers import add_external_server, update_external_server
    from api.network_servers import AddServerBody, UpdateExternalServerBody
    from llm.endpoint_policy import endpoint_agent_use

    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({_CHAT: _resp(200)}))
    sid = asyncio.run(add_external_server(
        AddServerBody(
            name="Restricted", base_url=_BASE, dialect="openai", model_ids=["glm-5.3"],
            agent_use_restriction="interactive_only",
        ),
    ))["server_id"]

    assert _server_meta(isolated_db, sid)["agent_use_restriction"] == "interactive_only"
    assert endpoint_agent_use(f"external@{sid}/glm-5.3") == "interactive_only"

    asyncio.run(update_external_server(
        sid, UpdateExternalServerBody(base_url=_BASE, agent_use_restriction="allowed"),
    ))

    assert _server_meta(isolated_db, sid)["agent_use_restriction"] == "allowed"
    assert endpoint_agent_use(f"external@{sid}/glm-5.3") == "allowed"


def test_moving_an_endpoint_onto_a_token_plan_host_restricts_it(isolated_db, monkeypatch) -> None:
    from api.network_server_helpers import add_external_server, update_external_server
    from api.network_servers import AddServerBody, UpdateExternalServerBody
    from llm.endpoint_policy import endpoint_agent_use

    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({_CHAT: _resp(200)}))
    sid = asyncio.run(add_external_server(
        AddServerBody(
            name="Workspace", base_url="https://api.example.com/compatible-mode/v1",
            dialect="openai", model_ids=["glm-5.3"],
        ),
    ))["server_id"]
    assert endpoint_agent_use(f"external@{sid}/glm-5.3") == "allowed"

    asyncio.run(update_external_server(sid, UpdateExternalServerBody(base_url=_BASE)))

    assert _server_meta(isolated_db, sid)["agent_use_restriction"] == "interactive_only"
    assert endpoint_agent_use(f"external@{sid}/glm-5.3") == "interactive_only"


def test_an_edit_that_stays_on_its_host_keeps_a_chosen_restriction(isolated_db, monkeypatch) -> None:
    from api.network_server_helpers import add_external_server, update_external_server
    from api.network_servers import AddServerBody, UpdateExternalServerBody
    from llm.endpoint_policy import endpoint_agent_use

    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({_CHAT: _resp(200)}))
    sid = asyncio.run(add_external_server(
        AddServerBody(
            name="Workspace", base_url=_BASE, dialect="openai", model_ids=["glm-5.3"],
            agent_use_restriction="allowed",
        ),
    ))["server_id"]

    asyncio.run(update_external_server(sid, UpdateExternalServerBody(base_url=_BASE)))

    assert endpoint_agent_use(f"external@{sid}/glm-5.3") == "allowed"


def test_a_re_add_without_the_field_keeps_the_stored_restriction(isolated_db, monkeypatch) -> None:
    from api.network_server_helpers import add_external_server
    from api.network_servers import AddServerBody
    from llm.endpoint_policy import endpoint_agent_use

    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({_CHAT: _resp(200)}))
    body = dict(name="Restricted", base_url=_BASE, dialect="openai", model_ids=["glm-5.3"])
    sid = asyncio.run(add_external_server(
        AddServerBody(**body, agent_use_restriction="interactive_only"),
    ))["server_id"]

    again = asyncio.run(add_external_server(AddServerBody(**body)))["server_id"]

    assert again == sid
    assert endpoint_agent_use(f"external@{sid}/glm-5.3") == "interactive_only"


def test_a_new_token_plan_endpoint_is_stored_interactive_only(isolated_db, monkeypatch) -> None:
    from api.network_server_helpers import add_external_server
    from api.network_servers import AddServerBody
    from llm.endpoint_policy import endpoint_agent_use

    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({_CHAT: _resp(200)}))
    sid = asyncio.run(add_external_server(
        AddServerBody(name="Workspace", base_url=_BASE, dialect="openai", model_ids=["glm-5.3"]),
    ))["server_id"]

    assert _server_meta(isolated_db, sid)["agent_use_restriction"] == "interactive_only"
    assert endpoint_agent_use(f"external@{sid}/glm-5.3") == "interactive_only"


def test_re_adding_a_token_plan_endpoint_with_unreadable_metadata_restricts_it(
    isolated_db, monkeypatch,
) -> None:
    from api.network_server_helpers import add_external_server
    from api.network_servers import AddServerBody
    from llm.endpoint_policy import endpoint_agent_use

    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({_CHAT: _resp(200)}))
    body = dict(name="Workspace", base_url=_BASE, dialect="openai", model_ids=["glm-5.3"])
    sid = asyncio.run(add_external_server(AddServerBody(**body)))["server_id"]
    with closing(sqlite3.connect(str(isolated_db))) as db:
        db.execute("UPDATE network_servers SET metadata_json='{broken' WHERE id=?", (sid,))
        db.commit()

    asyncio.run(add_external_server(AddServerBody(**body)))

    assert _server_meta(isolated_db, sid)["agent_use_restriction"] == "interactive_only"
    assert endpoint_agent_use(f"external@{sid}/glm-5.3") == "interactive_only"


def test_a_registration_that_names_a_restriction_keeps_it(isolated_db, monkeypatch) -> None:
    from api.network_server_helpers import add_external_server
    from api.network_servers import AddServerBody
    from llm.endpoint_policy import endpoint_agent_use

    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({_CHAT: _resp(200)}))
    sid = asyncio.run(add_external_server(
        AddServerBody(
            name="Token Plan", base_url=_BASE, dialect="openai", model_ids=["glm-5.3"],
            agent_use_restriction="allowed",
        ),
    ))["server_id"]

    assert endpoint_agent_use(f"external@{sid}/glm-5.3") == "allowed"


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://token-plan.example.com/v1", "interactive_only"),
        ("https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1", "interactive_only"),
        ("https://api.example.com/v1", "allowed"),
        ("not a url", "allowed"),
    ],
)
def test_registration_default_reads_the_host(base_url, expected) -> None:
    from llm.endpoint_policy import registration_default

    assert registration_default(base_url) == expected


def test_a_token_plan_name_on_an_ordinary_host_stays_allowed(isolated_db, monkeypatch) -> None:
    from api.network_server_helpers import add_external_server
    from api.network_servers import AddServerBody
    from llm.endpoint_policy import endpoint_agent_use

    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({_CHAT: _resp(200)}))
    sid = asyncio.run(add_external_server(
        AddServerBody(
            name="Token Plan (Beijing)", base_url="https://api.example.com/compatible-mode/v1",
            dialect="openai", model_ids=["glm-5.3"],
        ),
    ))["server_id"]

    assert "agent_use_restriction" not in _server_meta(isolated_db, sid)
    assert endpoint_agent_use(f"external@{sid}/glm-5.3") == "allowed"


# ── removal stays removed ────────────────────────────────────────────────────

def _seed_removable(path) -> None:
    with closing(sqlite3.connect(str(path))) as db:
        db.execute(
            "INSERT INTO network_servers "
            "(id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at) "
            "VALUES ('ns-gone','external','external','i','Gone',443,'online','t0','t0','t0')"
        )
        db.execute(
            "INSERT INTO models (id,provider,provider_model_id,litellm_model,display_name,"
            "enabled,available,created_at,updated_at) "
            "VALUES ('ext-gone','external','glm-5.3','external@ns-gone/glm-5.3','glm',1,1,'t0','t0')"
        )
        db.commit()


def _removed_state(path) -> tuple:
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute(
            "SELECT s.status, m.available FROM network_servers s, models m "
            "WHERE s.id='ns-gone' AND m.id='ext-gone'"
        ).fetchone()


def test_removing_a_server_withdraws_its_models(isolated_db) -> None:
    from llm import network_discovery as nd

    _seed_removable(isolated_db)
    assert nd.remove_server("ns-gone") is True
    assert _removed_state(isolated_db) == ("removed", 0)
    assert nd.remove_server("ns-missing") is False


@pytest.mark.parametrize("status", ["online", "offline"])
def test_a_sweep_that_read_the_server_before_removal_cannot_revive_it(
    isolated_db, status: str,
) -> None:
    from llm import network_discovery as nd

    _seed_removable(isolated_db)
    nd.remove_server("ns-gone")
    # The sweep's write lands after the removal committed.
    nd.update_server_status("ns-gone", status=status, latency_ms=5)
    assert _removed_state(isolated_db) == ("removed", 0)
    assert all(s["id"] != "ns-gone" for s in nd.get_servers())


# ── an endpoint switched off stays off ───────────────────────────────────────

def test_switching_a_server_off_withdraws_its_models_until_switched_on(isolated_db) -> None:
    from llm import network_discovery as nd

    _seed_removable(isolated_db)
    assert nd.set_server_enabled("ns-gone", False) is True
    assert _removed_state(isolated_db) == ("disabled", 0)
    # Still listed, so its card can be switched back on.
    assert any(s["id"] == "ns-gone" for s in nd.get_servers())
    # A sweep write that lands now cannot turn it back on.
    nd.update_server_status("ns-gone", status="online", latency_ms=5)
    assert _removed_state(isolated_db) == ("disabled", 0)

    assert nd.set_server_enabled("ns-gone", True) is True
    assert _removed_state(isolated_db)[0] == "offline"  # the next probe decides online
    nd.update_server_status("ns-gone", status="online", latency_ms=5)
    assert _removed_state(isolated_db)[0] == "online"


def test_a_removed_or_unknown_server_cannot_be_switched(isolated_db) -> None:
    from llm import network_discovery as nd

    _seed_removable(isolated_db)
    nd.remove_server("ns-gone")
    assert nd.set_server_enabled("ns-gone", True) is False
    assert nd.set_server_enabled("ns-missing", False) is False
    assert _removed_state(isolated_db) == ("removed", 0)


def test_editing_a_switched_off_endpoint_leaves_it_off(isolated_db, monkeypatch) -> None:
    from api.network_server_helpers import add_external_server, update_external_server
    from api.network_servers import AddServerBody, UpdateExternalServerBody
    from llm import network_discovery as nd

    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({_CHAT: _resp(200)}))
    sid = asyncio.run(add_external_server(
        AddServerBody(name="TP", base_url=_BASE, dialect="openai", model_ids=["glm-5.3"]),
    ))["server_id"]
    nd.set_server_enabled(sid, False)
    asyncio.run(update_external_server(sid, UpdateExternalServerBody(base_url=_BASE)))
    assert nd.get_server(sid)["status"] == "disabled"
