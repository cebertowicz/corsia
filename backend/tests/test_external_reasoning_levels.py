"""External models take their reasoning levels from the public models.dev catalog.

The endpoint cannot report them, so discovery matches the endpoint's base URL
and the model id against the catalog. Only a level the catalog lists is ever
sent; anything else sends nothing and the provider's default runs.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from llm import public_model_catalog as pmc

_TOKEN_PLAN = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
_CATALOG = {
    "alibaba-token-plan": {
        "api": _TOKEN_PLAN,
        "models": {
            "glm-5.3": {"reasoning": True, "reasoning_options": [
                {"type": "effort", "values": ["low", "high", "max"]},
            ]},
            "deepseek-v4.1-flash": {"reasoning": True, "reasoning_options": [
                {"type": "toggle"}, {"type": "effort", "values": ["low", "high", "max"]},
            ]},
            "glm-5.1": {"reasoning": True, "reasoning_options": [{"type": "toggle"}]},
        },
    },
    "elsewhere": {
        "api": "https://api.example.com/v1",
        "models": {"glm-5.3": {"reasoning_options": [
            {"type": "effort", "values": ["none", "minimal", "low", "medium", "high"]},
        ]}},
    },
}


# ---- matching ---------------------------------------------------------------

def test_levels_come_from_the_provider_that_publishes_this_base_url() -> None:
    shape = pmc.reasoning_shape(_CATALOG, _TOKEN_PLAN + "/", "glm-5.3")
    assert shape == {
        "type": "effort_levels",
        "values": ["low", "high", "max"],
        "default": None,
        "supports_auto": True,
        "supports_off": False,
        "source": {"catalog": "models.dev", "provider": "alibaba-token-plan"},
    }


def test_the_same_model_on_another_host_has_that_hosts_levels() -> None:
    shape = pmc.reasoning_shape(_CATALOG, "https://api.example.com/v1", "glm-5.3")
    assert shape["values"] == ["none", "minimal", "low", "medium", "high"]
    assert shape["supports_off"] is True


@pytest.mark.parametrize("base, model", [
    (_TOKEN_PLAN, "glm-5.1"),           # listed, but no effort levels
    (_TOKEN_PLAN, "unlisted-model"),    # not listed
    ("https://unknown.example/v1", "glm-5.3"),  # no provider publishes this base
])
def test_unknown_levels_stay_unknown(base, model) -> None:
    assert pmc.reasoning_shape(_CATALOG, base, model) is None


# ---- fetching ---------------------------------------------------------------

def _serve(monkeypatch, tmp_path, handler) -> None:
    monkeypatch.setenv("CORSIA_MODEL_CATALOG_URL", pmc.CATALOG_URL)
    monkeypatch.setattr(pmc, "_cache_path", lambda: tmp_path / "catalog.cache")
    real_client = httpx.AsyncClient

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(pmc.httpx, "AsyncClient", client)


def test_a_fetched_catalog_is_cached_and_revalidated_by_etag(monkeypatch, tmp_path) -> None:
    seen: list[str | None] = []

    def handler(request):
        seen.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json=_CATALOG, headers={"etag": '"v1"'})

    _serve(monkeypatch, tmp_path, handler)
    assert asyncio.run(pmc.load_catalog()) == (_CATALOG, None)
    assert asyncio.run(pmc.load_catalog()) == (_CATALOG, None)
    assert seen == [None, '"v1"']


def test_a_failed_refresh_uses_the_last_good_copy_and_says_so(monkeypatch, tmp_path) -> None:
    (tmp_path / "catalog.cache").write_text("\n" + json.dumps(_CATALOG))
    _serve(monkeypatch, tmp_path, lambda request: httpx.Response(503))

    catalog, warning = asyncio.run(pmc.load_catalog())
    assert catalog == _CATALOG
    assert "503" in warning and "last good copy" in warning


def test_a_refetch_without_an_etag_drops_the_old_etag_with_its_body(monkeypatch, tmp_path) -> None:
    (tmp_path / "catalog.cache").write_text('"old"\n' + json.dumps({"stale": {}}))
    _serve(monkeypatch, tmp_path, lambda request: httpx.Response(200, json=_CATALOG))

    assert asyncio.run(pmc.load_catalog()) == (_CATALOG, None)
    assert pmc._read_cached() == (_CATALOG, None)
    assert [p.name for p in tmp_path.iterdir()] == ["catalog.cache"]


def test_the_catalog_can_be_turned_off(monkeypatch) -> None:
    monkeypatch.setenv("CORSIA_MODEL_CATALOG_URL", "")
    assert asyncio.run(pmc.reasoning_shapes(_TOKEN_PLAN, ["glm-5.3"])) == (None, None)


def test_no_copy_at_all_leaves_levels_unknown_and_reports_why(monkeypatch, tmp_path) -> None:
    _serve(monkeypatch, tmp_path, lambda request: httpx.Response(503))

    shapes, warning = asyncio.run(pmc.reasoning_shapes(_TOKEN_PLAN, ["glm-5.3"]))
    assert shapes is None
    assert "models.dev could not be read" in warning


# ---- discovery writes the levels --------------------------------------------

_CAPS = {"supports_vision": 0, "supports_tools": 1, "supports_reasoning": 0}


def _seed_server(path, sid: str) -> None:
    with closing(sqlite3.connect(str(path))) as db:
        db.execute(
            "INSERT INTO network_servers (id,provider,kind,dialect,instance_id,"
            "instance_name,port,status,last_seen_at,created_at,updated_at) "
            "VALUES (?,'external','external','openai',?,?,443,'online','t0','t0','t0')",
            (sid, f"{sid}.example.com:443/v1", sid),
        )
        db.commit()


def _reconcile(sid: str, model_ids: list[str], shapes) -> dict:
    from llm import external_provider as ep

    server = {"id": sid, "instance_name": sid, "status": "online"}
    result = {"ok": False, "discovered": 0, "updated": 0, "missing": 0}
    return ep._reconcile_external_models(
        sid, server, "openai", model_ids, _CAPS, "t1", result, "", None, shapes,
    )


def _shape(path, pmid: str):
    with closing(sqlite3.connect(str(path))) as db:
        meta = db.execute(
            "SELECT metadata_json FROM models WHERE litellm_model = ?",
            (f"external@ns-plan/{pmid}",),
        ).fetchone()[0]
    return json.loads(meta).get("reasoning_shape")


def test_discovery_stores_levels_and_the_picker_offers_them(isolated_db) -> None:
    from llm.external_provider import build_external_params
    from llm.model_status import _capabilities_for_row
    from llm.reasoning_config import reasoning_config_for

    _seed_server(isolated_db, "ns-plan")
    glm = pmc.reasoning_shape(_CATALOG, _TOKEN_PLAN, "glm-5.3")
    _reconcile("ns-plan", ["glm-5.3", "other"], {"glm-5.3": glm, "other": None})

    assert _shape(isolated_db, "glm-5.3") == glm
    assert _shape(isolated_db, "other") is None
    assert reasoning_config_for("external", "ns-plan/glm-5.3") == glm

    known = _capabilities_for_row("native", "external", {"provider_model_id": "ns-plan/glm-5.3"}, {}, None)
    unknown = _capabilities_for_row("native", "external", {"provider_model_id": "ns-plan/other"}, {}, None)
    assert known["reasoning_config"]["values"] == ["low", "high", "max"]
    assert known["reasoning_levels_unknown"] is False
    assert unknown["reasoning_config"] is None
    assert unknown["reasoning_levels_unknown"] is True

    from unittest.mock import patch
    with patch("llm.network_endpoints.build_api_base", return_value=_TOKEN_PLAN):
        assert build_external_params("ns-plan", "glm-5.3")["reasoning_levels"] == ["low", "high", "max"]
        assert build_external_params("ns-plan", "other")["reasoning_levels"] is None


def test_two_endpoints_serving_one_model_id_keep_their_own_levels(isolated_db) -> None:
    """The picker and the wire read the same row: this endpoint's, never another's."""
    from unittest.mock import patch

    from llm.external_provider import build_external_params
    from llm.model_status import _capabilities_for_row

    _seed_server(isolated_db, "ns-plan")
    _seed_server(isolated_db, "ns-other")
    plan = {"type": "effort_levels", "values": ["low", "high", "max"], "default": "high"}
    other = {"type": "effort_levels", "values": ["low", "medium"], "default": "medium"}
    _reconcile("ns-plan", ["glm-5.3"], {"glm-5.3": plan})
    _reconcile("ns-other", ["glm-5.3"], {"glm-5.3": other})

    for sid, shape in (("ns-plan", plan), ("ns-other", other)):
        row = {"provider_model_id": f"{sid}/glm-5.3",
               "litellm_model": f"external@{sid}/glm-5.3"}
        picker = _capabilities_for_row("native", "external", row, {}, None)
        assert picker["reasoning_config"]["values"] == shape["values"]
        with patch("llm.network_endpoints.build_api_base", return_value=_TOKEN_PLAN):
            wire = build_external_params(sid, "glm-5.3")["reasoning_levels"]
        assert wire == shape["values"]


def test_an_unreadable_catalog_keeps_the_levels_already_stored(isolated_db) -> None:
    _seed_server(isolated_db, "ns-plan")
    glm = pmc.reasoning_shape(_CATALOG, _TOKEN_PLAN, "glm-5.3")
    _reconcile("ns-plan", ["glm-5.3"], {"glm-5.3": glm})

    _reconcile("ns-plan", ["glm-5.3"], None)
    assert _shape(isolated_db, "glm-5.3") == glm

    # A catalog that was read and no longer lists levels clears them.
    _reconcile("ns-plan", ["glm-5.3"], {"glm-5.3": None})
    assert _shape(isolated_db, "glm-5.3") is None


# ---- the level on the wire --------------------------------------------------

def test_a_saved_level_the_model_does_not_list_falls_back_to_the_provider_default(monkeypatch) -> None:
    from llm import reasoning_config as rc

    glm = pmc.reasoning_shape(_CATALOG, _TOKEN_PLAN, "glm-5.3")
    monkeypatch.setattr(rc, "reasoning_config_for_model", lambda m: glm)
    assert rc.effective_reasoning_effort("max", "ext-glm") == "max"
    assert rc.effective_reasoning_effort("medium", "ext-glm") is None


class _CaptureHandler(BaseHTTPRequestHandler):
    bodies: list[dict] = []

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length") or 0)
        type(self).bodies.append(json.loads(self.rfile.read(length) or b"{}"))
        reply = json.dumps({
            "id": "c1", "object": "chat.completion", "created": 0, "model": "glm-5.3",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


def _send(effort: str | None, levels: list[str] | None) -> dict:
    from llm import router

    _CaptureHandler.bodies = []
    server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(router.chat_completion(
            [{"role": "user", "content": "hi"}],
            None,
            model="openai/glm-5.3",
            api_key="sk-test",
            api_base=f"http://127.0.0.1:{server.server_port}/v1",
            reasoning_effort=effort,
            reasoning_levels=levels,
            retry_transient_errors=False,
        ))
    finally:
        server.shutdown()
        thread.join()
    assert len(_CaptureHandler.bodies) == 1
    return _CaptureHandler.bodies[0]


def test_a_listed_level_goes_on_the_wire_as_top_level_reasoning_effort() -> None:
    body = _send("low", ["low", "high", "max"])
    assert body["reasoning_effort"] == "low"


@pytest.mark.parametrize("effort, levels", [
    (None, ["low", "high", "max"]),      # nothing chosen: provider default
    ("auto", ["low", "high", "max"]),
    ("medium", ["low", "high", "max"]),  # not a level this model lists
    ("none", ["low", "high", "max"]),
    ("high", None),                      # levels unknown
])
def test_anything_else_sends_no_level(effort, levels) -> None:
    assert "reasoning_effort" not in _send(effort, levels)
