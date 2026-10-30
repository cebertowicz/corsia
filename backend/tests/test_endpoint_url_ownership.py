"""One owner for provider call URLs, and the identity that keys an endpoint.

These are the paths that shipped unexercised: the validator that refuses a
credential-bearing URL, the composition every caller now shares, the registration
that must persist exactly what it probed, and the base parameter resolution uses.
The shape of the failure this file exists for is quiet — a call site naming a
symbol the branch never defined, so every registration 500s while the tests that
would have caught it test a dataclass instead.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import re
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from llm import network_endpoints as ne
from llm.network_endpoints import mgmt_status_url, models_probe_url, validated_base_url
from omlx_provider_fixtures import _patch_httpx, _resp, _Router

_OPENAI_BASE = "https://ws01.us-east-1.maas.example.com/compatible-mode/v1"
_ANTHROPIC_BASE = "https://api.anthropic.com"
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


# ── the branch defines what its call sites call ──────────────────────────────

@pytest.mark.parametrize("module_name", [
    "api.network_server_helpers", "api.network_daemon_models", "llm.network_discovery",
    "llm.inference_params",
])
def test_every_attribute_a_call_site_reaches_exists(module_name: str) -> None:
    """A renamed function and a stale call site must not survive to main."""
    source = inspect.getsource(importlib.import_module(module_name))
    referenced = set(re.findall(r"\bne\.([A-Za-z_]\w*)", source))
    missing = [name for name in sorted(referenced) if not hasattr(ne, name)]
    assert not missing, f"{module_name} reaches ne.{missing[0]}, which is not defined"


# ── the validator ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    _OPENAI_BASE,
    _ANTHROPIC_BASE,
    "http://100.64.1.5:8000/v1",
])
def test_a_scheme_qualified_url_is_accepted_verbatim(url: str) -> None:
    assert validated_base_url(url) == url


def test_a_trailing_slash_is_the_only_thing_stripped() -> None:
    assert validated_base_url(f"{_OPENAI_BASE}/") == _OPENAI_BASE


@pytest.mark.parametrize("url", [
    "ws://host/v1",                      # not http(s)
    "host/compatible-mode/v1",           # no scheme
    "//host/v1",                         # no scheme at all
    "https://user:sekret@host/v1",       # credentials in the URL
    "https://host/v1?key=abc",           # a query the user would lose silently
    "https://host/v1#frag",              # a fragment likewise
])
def test_a_url_we_could_misread_is_refused_rather_than_repaired(url: str) -> None:
    assert validated_base_url(url) is None


# ── the one composition ──────────────────────────────────────────────────────

@pytest.mark.parametrize("base,expected", [
    (_OPENAI_BASE, "https://ws01.us-east-1.maas.example.com/compatible-mode/v1/models"),
    # a provider's own version segment is kept, never rewritten to /v1
    (_GEMINI_BASE, "https://generativelanguage.googleapis.com/v1beta/openai/models"),
    ("https://ark.example.com/api/v3", "https://ark.example.com/api/v3/models"),
    # a bare host that serves /models directly is called there
    ("https://api.deepseek.example.com", "https://api.deepseek.example.com/models"),
    ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1/models"),
])
def test_an_openai_base_is_used_exactly_as_typed(base: str, expected: str) -> None:
    assert models_probe_url(base) == expected


@pytest.mark.parametrize("base", [_ANTHROPIC_BASE, f"{_ANTHROPIC_BASE}/v1"])
def test_the_anthropic_catalog_is_at_its_versioned_root(base: str) -> None:
    assert models_probe_url(base, dialect="anthropic") == "https://api.anthropic.com/v1/models"


def test_the_legacy_host_port_shape_is_unchanged() -> None:
    assert models_probe_url(ne.lan_base("10.0.0.5", 5200)) == "http://10.0.0.5:5200/v1/models"


# —— one reading of the stored base ————————————————————————————————————————————————————————

def test_a_verbatim_base_always_beats_the_host_port_composition() -> None:
    """An https endpoint probed over http:// fails its handshake and looks dead."""
    meta = {"base_url": _OPENAI_BASE}
    assert ne.endpoint_base(meta, host="ws01.example.com", port=443) == _OPENAI_BASE


def test_no_stored_base_is_the_lan_composition() -> None:
    assert ne.endpoint_base({}, host="10.0.0.5", port=5200) == "http://10.0.0.5:5200/v1"


def test_an_invalid_stored_base_is_unreachable_not_re_guessed() -> None:
    """Falling back to http://host:port would probe a server nobody registered."""
    meta = {"base_url": "https://host/v1?key=abc"}
    assert ne.endpoint_base(meta, host="host", port=443) is None
    assert ne.external_probe_url(meta, dialect="openai", host="host", port=443) is None


@pytest.mark.parametrize("meta", [None, {}, {"api_key": "k"}])
def test_an_external_row_without_a_readable_base_is_never_re_derived(meta) -> None:
    """URL-registered rows have no host:port shape to fall back to; http:// would leak the key."""
    assert ne.endpoint_base(meta, host="api.example.com", port=443, external=True) is None
    assert ne.external_probe_url(meta, dialect="openai", host="api.example.com", port=443) is None


@pytest.mark.parametrize("host", ["127.0.0.1", "192.168.1.20", "100.101.102.103"])
def test_a_private_host_external_row_registered_by_host_port_keeps_its_base(host) -> None:
    """Rows added by host:port before URL registration have no base_url to read."""
    assert ne.endpoint_base({}, host=host, port=9000, external=True) == (
        f"http://{host}:9000/v1"
    )


def test_unreadable_metadata_makes_an_external_server_unreachable() -> None:
    assert ne.endpoint_base(None, host="10.0.0.5", port=5200, external=True) is None


def test_unreadable_metadata_keeps_a_managed_server_on_its_lan_base() -> None:
    assert ne.endpoint_base(None, host="10.0.0.5", port=5200) == "http://10.0.0.5:5200/v1"


def _external_server_rows(monkeypatch, meta: dict, *, dialect: str = "openai") -> dict:
    from llm import network_discovery

    server = {"id": "ns-ext-1", "kind": "external", "dialect": dialect, "port": 443,
              "status": "online", "provider": "external"}
    monkeypatch.setattr(network_discovery, "get_server_metadata", lambda sid: meta or {})
    monkeypatch.setattr(network_discovery, "read_server_metadata", lambda sid: meta)
    monkeypatch.setattr(network_discovery, "get_server", lambda sid: server)
    monkeypatch.setattr(ne, "get_endpoints", lambda sid: [{"host": "api.example.com"}])
    monkeypatch.setattr(network_discovery, "get_endpoints", lambda sid: [{"host": "api.example.com"}])
    return server


@pytest.mark.parametrize("meta,expected", [
    ({"base_url": _GEMINI_BASE}, [f"{_GEMINI_BASE}/models"]),
    ({"base_url": "https://host/v1?key=abc"}, []),
    (None, []),
    ({"api_key": "k"}, []),
])
def test_the_sweep_probes_the_base_dispatch_would_call(monkeypatch, meta, expected) -> None:
    from llm import network_discovery

    server = _external_server_rows(monkeypatch, meta)
    offline: list = []
    monkeypatch.setattr(network_discovery, "upsert_endpoint", lambda *a, **k: None)
    monkeypatch.setattr(network_discovery, "update_server_status", lambda *a, **k: None)
    monkeypatch.setattr(network_discovery, "_record_server_offline",
                        lambda srv, provider: offline.append(srv["id"]))
    monkeypatch.setattr(network_discovery, "_notify_models_changed_safe", lambda: None)
    router = _Router({("GET", "/v1beta/openai/models"): _resp(200, {"data": []})})

    async def _run() -> None:
        import httpx
        _patch_httpx(monkeypatch, router)
        async with httpx.AsyncClient() as client:
            await network_discovery._sweep_external(client, server)

    asyncio.run(_run())
    assert [url for _m, url, _h in router.calls] == expected
    assert offline == ([] if expected else ["ns-ext-1"])


@pytest.mark.parametrize("meta", [
    {"base_url": "https://host/v1?key=abc"}, None, {"api_key": "k"},
])
def test_the_freshness_guard_never_probes_a_guessed_url(monkeypatch, meta) -> None:
    _external_server_rows(monkeypatch, meta)
    monkeypatch.setattr(ne, "get_active_endpoint", lambda sid: None)
    monkeypatch.setattr(ne, "_server_port", lambda sid: 443)
    router = _Router({})
    _patch_httpx(monkeypatch, router)
    from llm import network_discovery
    monkeypatch.setattr(network_discovery, "update_server_status", lambda *a, **k: None)

    assert asyncio.run(ne.freshness_guard("ns-ext-1")) is False
    assert router.calls == []


def test_the_managed_status_url_has_one_owner_too() -> None:
    assert mgmt_status_url("10.0.0.5", 5200) == "http://10.0.0.5:5200/api/status"


# ── registration persists what it reached ────────────────────────────────────

# Product events the last _register call emitted, as (feature, action, properties).
_EVENTS: list[tuple[str, str, dict | None]] = []


def _register(monkeypatch, base: str, *, dialect: str | None = "openai", path_reply=None,
              warm: int | None = 3600, api_key: str | None = None, transport=None):
    from api.network_server_helpers import add_external_server
    from api.network_servers import AddServerBody
    from llm import network_discovery

    router = _Router({("GET", "/v1/models"): _resp(200, {"data": [{"id": "m"}]})})
    if path_reply is not None:
        router = _Router(path_reply)
    _patch_httpx(monkeypatch, transport or router)

    from infra import product_events

    upserted: list = []
    stored: list = []
    events = _EVENTS
    events.clear()

    def _fake_upsert(**kw: object) -> str:
        upserted.append(kw)
        return "ns-ext-1"

    monkeypatch.setattr(network_discovery, "upsert_server", _fake_upsert)
    monkeypatch.setattr(
        network_discovery, "set_server_metadata",
        lambda sid, *, db, clear=(), **kw: stored.append((sid, kw, clear)),
    )
    monkeypatch.setattr(network_discovery, "discover_and_notify", lambda sid: asyncio.sleep(0))
    monkeypatch.setattr(
        product_events, "emit_product_event",
        lambda feature, action, **kw: events.append((feature, action, kw.get("properties"))),
    )

    body = AddServerBody(name="Prod", base_url=base, dialect=dialect,
                         cache_warm_seconds=warm, api_key=api_key)
    out = asyncio.run(add_external_server(body))
    return out, router, upserted, stored


def test_a_registered_endpoint_is_probed_at_the_url_it_will_be_called_through(monkeypatch) -> None:
    out, router, upserted, stored = _register(monkeypatch, _OPENAI_BASE)

    assert out["ok"] is True and out["server_id"] == "ns-ext-1"
    requested = [url for _method, url, _h in router.calls]
    assert requested == [models_probe_url(_OPENAI_BASE)], requested
    assert requested[0].startswith("https://"), requested


def test_a_gemini_shaped_base_registers_at_its_own_version_path(monkeypatch) -> None:
    out, router, _upserted, stored = _register(
        monkeypatch, _GEMINI_BASE,
        path_reply={("GET", "/v1beta/openai/models"): _resp(200, {"data": [{"id": "g"}]})},
    )

    assert out["ok"] is True
    assert [url for _m, url, _h in router.calls] == [f"{_GEMINI_BASE}/models"]
    assert stored[0][1]["base_url"] == _GEMINI_BASE


def test_a_missing_version_path_is_named_not_guessed(monkeypatch) -> None:
    with pytest.raises(HTTPException) as exc:
        _register(monkeypatch, "https://api.example.com",
                  path_reply={("GET", "/v1/models"): _resp(200, {"data": []})})

    assert "404" in exc.value.detail and "version path" in exc.value.detail


def test_registration_stores_the_base_verbatim_with_scheme_and_path(monkeypatch) -> None:
    _out, _router, upserted, stored = _register(monkeypatch, _OPENAI_BASE)

    assert upserted[0]["kind"] == "external"
    assert upserted[0]["dialect"] == "openai"
    meta = stored[0][1]
    assert meta["base_url"] == _OPENAI_BASE
    assert meta["cache_warm_seconds"] == 3600


def test_the_url_path_is_part_of_the_endpoints_identity(monkeypatch) -> None:
    """Two workspaces behind one host are two endpoints, not one overwritten row."""
    _out, _router, first, _s = _register(
        monkeypatch, "https://gw.example.com/workspace-a/v1")
    _out, _router, second, _s = _register(
        monkeypatch, "https://gw.example.com/workspace-b/v1")

    assert first[0]["instance_id"] != second[0]["instance_id"]
    assert "workspace-a" in first[0]["instance_id"]


def test_an_anthropic_endpoint_is_probed_at_its_native_catalog_url(monkeypatch) -> None:
    _out, router, upserted, _stored = _register(
        monkeypatch, _ANTHROPIC_BASE, dialect="anthropic", warm=None)

    assert [url for _m, url, _h in router.calls] == [
        models_probe_url(_ANTHROPIC_BASE, dialect="anthropic")]
    assert upserted[0]["dialect"] == "anthropic"


def test_an_anthropic_compatible_endpoint_keeps_its_declared_window(monkeypatch) -> None:
    """The dialect is the wire format; the provider behind it owns its cache."""
    _out, _router, _upserted, stored = _register(
        monkeypatch, "https://gw.example.com/apps/anthropic", dialect="anthropic", warm=3600)

    assert stored[0][1]["cache_warm_seconds"] == 3600


@pytest.mark.parametrize("warm", [0, -1, 86_401, 10**12])
def test_an_out_of_range_window_is_refused_at_the_api(warm: int) -> None:
    from pydantic import ValidationError

    from api.network_servers import AddServerBody

    with pytest.raises(ValidationError):
        AddServerBody(name="x", base_url=_OPENAI_BASE, cache_warm_seconds=warm)


def test_an_unreachable_endpoint_is_refused_with_the_url_and_reason(monkeypatch) -> None:
    with pytest.raises(HTTPException) as exc:
        _register(monkeypatch, _OPENAI_BASE,
                  path_reply={("GET", "/v1/models"): _resp(401)})

    assert exc.value.status_code == 400
    assert "401" in exc.value.detail and "/v1/models" in exc.value.detail
    assert _EVENTS == [
        ("external_endpoint", "register.failed", {"dialect": "openai", "reason": "http_401"}),
    ]


def test_a_re_registration_is_the_whole_statement_for_the_endpoint(monkeypatch) -> None:
    """A window or key left empty on re-add is cleared, not kept from last time."""
    _out, _router, _upserted, stored = _register(monkeypatch, _OPENAI_BASE, warm=None)
    assert stored[0][2] == ("cache_warm_seconds", "declared_models")

    _out, _router, _upserted, stored = _register(
        monkeypatch, _OPENAI_BASE, warm=600, api_key="sk-new")
    assert stored[0][2] == ("declared_models",)
    assert stored[0][1]["cache_warm_seconds"] == 600


def test_a_successful_registration_is_measured(monkeypatch) -> None:
    _register(monkeypatch, _OPENAI_BASE)
    assert _EVENTS == [("external_endpoint", "registered", {
        "dialect": "openai", "models": 1, "declared_cache_window": True,
        "declared_models": 0,
    })]


@pytest.mark.asyncio
async def test_updating_an_external_endpoint_retains_its_model_settings_and_key(
    isolated_db, monkeypatch,
) -> None:
    """An edit changes endpoint configuration, never its stable model identity."""
    from api import network_server_helpers as helpers
    from api.network_servers import UpdateExternalServerBody
    from infra.database import get_db
    from llm import network_discovery as nd

    sid = nd.upsert_server(
        provider="external", instance_id="old.example.com/v1", instance_name="External",
        host="old.example.com", port=443, status="online", kind="external", dialect="openai",
    )
    nd.set_server_metadata(sid, base_url="https://old.example.com/v1", api_key="stored-key")
    db = get_db()
    db.execute(
        """INSERT INTO models
           (id, provider, provider_model_id, litellm_model, display_name, backend,
            source, enabled, available, context_window, created_at, updated_at)
           VALUES ('ext-test', 'external', 'model-a', ?, 'model-a', 'network',
                   'discovered', 0, 1, 123456, 't', 't')""",
        (f"external@{sid}/model-a",),
    )
    db.commit()
    monkeypatch.setattr(helpers, "_probe_external", AsyncMock(return_value=[{"id": "model-b"}]))
    monkeypatch.setattr(nd, "discover_and_notify", AsyncMock())

    # Same origin, new path: the saved key may follow, because it goes to the
    # host it was saved for.
    result = await helpers.update_external_server(
        sid,
        UpdateExternalServerBody(base_url="https://old.example.com/v2", dialect="anthropic"),
    )

    assert result == {"ok": True, "server_id": sid, "status": "online", "models": 1}
    server = nd.get_server(sid)
    assert server["dialect"] == "anthropic"
    assert server["instance_id"] == "old.example.com:443/v2"
    assert nd.get_server_metadata(sid)["base_url"] == "https://old.example.com/v2"
    assert nd.get_server_metadata(sid)["api_key"] == "stored-key"
    model = db.execute("SELECT enabled, context_window, litellm_model FROM models WHERE id = 'ext-test'").fetchone()
    assert tuple(model) == (0, 123456, f"external@{sid}/model-a")


@pytest.mark.asyncio
async def test_a_failed_external_endpoint_update_leaves_existing_state_unchanged(
    isolated_db, monkeypatch,
) -> None:
    from api import network_server_helpers as helpers
    from api.network_servers import UpdateExternalServerBody
    from llm import network_discovery as nd

    sid = nd.upsert_server(
        provider="external", instance_id="old.example.com/v1", instance_name="External",
        host="old.example.com", port=443, status="online", kind="external", dialect="openai",
    )
    nd.set_server_metadata(sid, base_url="https://old.example.com/v1", api_key="stored-key")
    monkeypatch.setattr(helpers, "_probe_external", AsyncMock(side_effect=HTTPException(400, "bad probe")))

    with pytest.raises(HTTPException, match="bad probe"):
        await helpers.update_external_server(
            sid,
            UpdateExternalServerBody(
                base_url="https://new.example.com/v1", dialect="anthropic", api_key="new-key",
            ),
        )

    server = nd.get_server(sid)
    assert server["instance_id"] == "old.example.com/v1"
    assert server["dialect"] == "openai"
    assert nd.get_server_metadata(sid) == {
        "base_url": "https://old.example.com/v1", "api_key": "stored-key",
    }


def test_a_transport_failure_is_named_and_measured(monkeypatch) -> None:
    import httpx

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name or service not known", request=request)

    with pytest.raises(HTTPException) as exc:
        _register(monkeypatch, _OPENAI_BASE, transport=_refuse)

    assert "Could not reach" in exc.value.detail and "ConnectError" in exc.value.detail
    assert _EVENTS[-1][2] == {"dialect": "openai", "reason": "ConnectError"}


def test_a_200_that_is_not_a_model_list_is_not_called_unreachable(monkeypatch) -> None:
    import httpx

    html = httpx.Response(200, text="<html>login</html>", request=httpx.Request("GET", "http://x/"))
    with pytest.raises(HTTPException) as exc:
        _register(monkeypatch, _OPENAI_BASE,
                  path_reply={("GET", "/compatible-mode/v1/models"): html})

    assert "not with a model list" in exc.value.detail
    assert "Could not reach" not in exc.value.detail
    assert _EVENTS[-1][2] == {"dialect": "openai", "reason": "unexpected_body"}


# ── dispatch and parameter resolution use the same owner ────────────────────

def _server(monkeypatch, *, base: str | None, dialect: str, live: bool = True,
            kind: str = "external", host: str = "api.anthropic.com",
            port: int = 443) -> None:
    from llm import network_discovery

    monkeypatch.setattr(network_discovery, "get_server",
                        lambda sid: {"id": sid, "port": port, "dialect": dialect, "kind": kind})
    monkeypatch.setattr(network_discovery, "get_server_metadata",
                        lambda sid: {"base_url": base, "api_key": "sk-test-1234"} if base else {})
    monkeypatch.setattr(ne, "get_active_endpoint",
                        lambda sid: {"host": host} if live else None)
    monkeypatch.setattr(ne, "_server_port", lambda sid: port)
    # Endpoint resolution reads the tri-state form; answer it from the same stub.
    patched_meta = network_discovery.get_server_metadata
    monkeypatch.setattr(network_discovery, "read_server_metadata", patched_meta)


def test_an_https_anthropic_endpoint_is_dispatched_over_https(monkeypatch) -> None:
    """Online at add time must mean the same URL every chat turn goes to."""
    from llm.external_provider import build_external_params

    _server(monkeypatch, base=_ANTHROPIC_BASE, dialect="anthropic")
    params = build_external_params("ns-ext-1", "claude-x")

    assert params["api_base"] == _ANTHROPIC_BASE
    assert models_probe_url(params["api_base"]) == models_probe_url(_ANTHROPIC_BASE)


def test_an_openai_endpoint_dispatches_to_the_base_it_was_probed_at(monkeypatch) -> None:
    from llm.external_provider import build_external_params

    _server(monkeypatch, base=_OPENAI_BASE, dialect="openai")
    params = build_external_params("ns-ext-1", "qwen-max")

    assert params["api_base"] == _OPENAI_BASE
    assert models_probe_url(params["api_base"]) == models_probe_url(_OPENAI_BASE)


@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
def test_a_known_offline_url_endpoint_is_not_dispatched_to(monkeypatch, dialect: str) -> None:
    from llm.external_provider import build_external_params

    _server(monkeypatch, base=_OPENAI_BASE, dialect=dialect, live=False)
    with pytest.raises(ValueError, match="no reachable endpoint"):
        build_external_params("ns-ext-1", "m")


def test_the_catalog_fetch_uses_the_one_composition(monkeypatch) -> None:
    from llm.external_provider import fetch_external_models

    router = _Router({("GET", "/v1/models"): _resp(200, {"data": [{"id": "qwen-max"}]})})
    _patch_httpx(monkeypatch, router)

    assert asyncio.run(fetch_external_models(_OPENAI_BASE, "openai", None)) == {"qwen-max": None}
    assert [url for _m, url, _h in router.calls] == [models_probe_url(_OPENAI_BASE)]


def test_the_catalog_fetch_keeps_each_reported_window(monkeypatch) -> None:
    from llm.external_provider import fetch_external_models

    listing = {"data": [{"id": "served", "object": "model", "max_model_len": 32768},
                        {"id": "id-only", "object": "model"}]}
    _patch_httpx(monkeypatch, _Router({("GET", "/v1/models"): _resp(200, listing)}))

    assert asyncio.run(fetch_external_models(_OPENAI_BASE, "openai", None)) == {
        "served": 32768, "id-only": None,
    }


def test_parameter_resolution_uses_the_openai_base_from_the_owner(monkeypatch) -> None:
    from llm import inference_params

    _server(monkeypatch, base=None, dialect="openai", kind="managed")
    assert inference_params._api_base_for_server("ns-omlx") == "http://api.anthropic.com:443/v1"


def test_a_legacy_loopback_external_row_is_still_dispatched_to(monkeypatch) -> None:
    from llm.external_provider import build_external_params

    _server(monkeypatch, base=None, dialect="openai", host="127.0.0.1", port=9000)
    assert ne.build_api_base("ns-ext-1") == "http://127.0.0.1:9000/v1"
    assert build_external_params("ns-ext-1", "m")["api_base"] == "http://127.0.0.1:9000/v1"


@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
def test_an_external_row_that_lost_its_base_is_not_dispatched_to(monkeypatch, dialect) -> None:
    """Unreadable or missing metadata must not turn an https endpoint into http://host:443."""
    from llm import network_discovery
    from llm.external_provider import build_external_params

    _server(monkeypatch, base=None, dialect=dialect)
    assert ne.build_api_base("ns-ext-1") is None
    monkeypatch.setattr(network_discovery, "read_server_metadata", lambda sid: None)
    assert ne.build_api_base("ns-ext-1") is None
    with pytest.raises(ValueError, match="no reachable endpoint"):
        build_external_params("ns-ext-1", "m")
