
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.local_backends import (  # noqa: E402
    ExternalBackend, OmlxBackend,
    find_backend, is_local, backend_name,
)


def test_external_matches():
    assert isinstance(find_backend("external@ns-1/qwen3-8b"), ExternalBackend)


def test_cloud_returns_none():
    assert find_backend("anthropic/claude-opus-4-7") is None
    assert find_backend("openai/gpt-4o") is None


def test_empty_returns_none():
    assert find_backend("") is None


def test_is_local_semantics():
    assert is_local("external@ns/X") is True
    assert is_local("omlx@srv/X") is True
    assert is_local("anthropic/claude") is False
    assert is_local("") is False


def test_backend_name():
    assert backend_name("external@ns/X") == "external"
    assert backend_name("omlx@a/b") == "omlx"
    assert backend_name("anthropic/x") is None


def test_external_parse_splits_server_and_model():
    server_id, model = ExternalBackend()._parse("external@ns-7/mlx-community/Model-8B")
    assert server_id == "ns-7"
    assert model == "mlx-community/Model-8B"


def test_external_transport_identity_uses_dialect_without_endpoint(monkeypatch):
    from llm import network_discovery, network_endpoints

    monkeypatch.setattr(network_discovery, "get_server", lambda sid: {
        "id": sid, "dialect": "anthropic",
    })
    monkeypatch.setattr(
        network_endpoints,
        "get_active_endpoint",
        lambda _sid: (_ for _ in ()).throw(AssertionError("endpoint resolved")),
    )

    assert ExternalBackend().transport_identity(
        "external@ns-2/claude-fable-5",
    ) == ("anthropic", "claude-fable-5")


def test_external_openai_dialect_params(monkeypatch):
    from llm import network_discovery, network_endpoints
    monkeypatch.setattr(network_discovery, "get_server", lambda sid: {
        "id": sid, "port": 1234, "dialect": "openai",
    })
    monkeypatch.setattr(network_discovery, "get_server_metadata",
                        lambda sid: {"api_key": "sk-tok"})
    monkeypatch.setattr(network_endpoints, "build_api_base",
                        lambda sid: "http://127.0.0.1:1234/v1")

    params = ExternalBackend().make_provider_params("external@ns-1/qwen3-8b")
    assert params["model"] == "openai/qwen3-8b"
    assert params["api_key"] == "sk-tok"
    assert params["api_base"] == "http://127.0.0.1:1234/v1"


def test_external_openai_keyless_uses_nonempty_sentinel(monkeypatch):
    """Keyless openai-dialect external MUST pass a non-empty api_key sentinel, not
    None; otherwise litellm falls back to OPENAI_API_KEY and misroutes a local
    endpoint to OpenAI's cloud (regression guard)."""
    from llm import network_discovery, network_endpoints
    monkeypatch.setattr(network_discovery, "get_server", lambda sid: {
        "id": sid, "port": 8090, "dialect": "openai",
    })
    monkeypatch.setattr(network_discovery, "get_server_metadata", lambda sid: {})  # no key
    monkeypatch.setattr(network_endpoints, "build_api_base",
                        lambda sid: "http://127.0.0.1:8090/v1")

    params = ExternalBackend().make_provider_params("external@ns-1/qwen")
    assert params["api_key"] == "sk-noauth"
    assert params["api_key"]  # non-empty


def test_external_openai_keyed_uses_real_key(monkeypatch):
    from llm import network_discovery, network_endpoints
    monkeypatch.setattr(network_discovery, "get_server", lambda sid: {
        "id": sid, "port": 443, "dialect": "openai",
    })
    monkeypatch.setattr(network_discovery, "get_server_metadata", lambda sid: {"api_key": "sk-real"})
    monkeypatch.setattr(network_endpoints, "build_api_base",
                        lambda sid: "https://api.example.com/v1")
    params = ExternalBackend().make_provider_params("external@ns-1/gpt")
    assert params["api_key"] == "sk-real"


def test_external_anthropic_keyless_uses_nonempty_sentinel(monkeypatch):
    """Keyless anthropic-dialect external MUST pass a non-empty api_key sentinel,
    not None — else the AsyncAnthropic SDK falls back to CORSIA's global
    ANTHROPIC_API_KEY env and leaks OUR real cloud key to the user endpoint
    (security regression guard)."""
    from llm import network_discovery, network_endpoints
    monkeypatch.setattr(network_discovery, "get_server", lambda sid: {
        "id": sid, "port": 8787, "dialect": "anthropic",
    })
    monkeypatch.setattr(network_discovery, "get_server_metadata", lambda sid: {})
    monkeypatch.setattr(
        network_endpoints, "get_active_endpoint", lambda sid: {"host": "10.0.0.4"}
    )
    monkeypatch.setattr(network_endpoints, "_server_port", lambda sid: 8787)
    params = ExternalBackend().make_provider_params("external@ns-2/claude-x")
    assert params["api_key"] == "sk-noauth"
    assert params["api_key"]  # non-empty — no env-key fallback


def test_external_anthropic_dialect_params(monkeypatch):
    from llm import network_discovery, network_endpoints
    monkeypatch.setattr(network_discovery, "get_server", lambda sid: {
        "id": sid, "port": 8787, "dialect": "anthropic",
    })
    monkeypatch.setattr(network_discovery, "get_server_metadata",
                        lambda sid: {"api_key": "sk-ant"})
    monkeypatch.setattr(network_endpoints, "get_active_endpoint",
                        lambda sid: {"host": "10.0.0.4"})
    monkeypatch.setattr(network_endpoints, "_server_port", lambda sid: 8787)

    params = ExternalBackend().make_provider_params("external@ns-2/claude-x")
    assert params["model"] == "anthropic/claude-x"
    assert params["api_key"] == "sk-ant"
    assert params["api_base"] == "http://10.0.0.4:8787"


def test_omlx_matches():
    assert isinstance(find_backend("omlx@ns-omlx-1/qwen3-vl"), OmlxBackend)


def test_omlx_is_local():
    assert is_local("omlx@ns-1/x") is True


def test_omlx_backend_name():
    assert backend_name("omlx@a/b") == "omlx"


def test_omlx_parse_splits_server_and_model():
    backend = OmlxBackend()
    server_id, model = backend._parse("omlx@ns-omlx-9/qwen3.5-vl-7b-mlx")
    assert server_id == "ns-omlx-9"
    assert model == "qwen3.5-vl-7b-mlx"


def test_omlx_transport_identity_uses_openai_without_endpoint(monkeypatch):
    from llm import network_endpoints

    monkeypatch.setattr(
        network_endpoints,
        "build_api_base",
        lambda _sid: (_ for _ in ()).throw(AssertionError("endpoint resolved")),
    )

    assert OmlxBackend().transport_identity(
        "omlx@ns/qwen3.5-vl",
    ) == ("openai", "qwen3.5-vl")


def test_native_transport_identity_reads_cloud_prefix_without_dispatch_params():
    from llm import capabilities

    assert capabilities.native_transport_identity(
        "anthropic/claude-fable-5",
    ) == ("anthropic", "claude-fable-5")


def test_omlx_provider_params_uses_openai_dispatch():
    from llm import network_endpoints
    import llm.local_backends as lb
    orig = network_endpoints.build_api_base
    network_endpoints.build_api_base = lambda sid: "http://127.0.0.1:5200/v1"
    try:
        params = OmlxBackend().make_provider_params("omlx@ns/qwen3.5-vl")
        assert params["model"] == "openai/qwen3.5-vl"
        assert params["api_key"] == "omlx"
        assert params["api_base"] == "http://127.0.0.1:5200/v1"
        assert params["uses_streaming"] is True
    finally:
        network_endpoints.build_api_base = orig
    _ = lb  # silence unused-import warning




def test_omlx_cfg_builds_from_network_server_row(monkeypatch):
    from llm import network_discovery, network_endpoints

    monkeypatch.setattr(network_discovery, "get_server", lambda sid: {
        "id": sid, "instance_name": "Studio", "port": 8000,
    } if sid == "ns-1" else None)
    monkeypatch.setattr(network_endpoints, "get_active_endpoint", lambda sid: {
        "host": "127.0.0.1",
    } if sid == "ns-1" else None)
    monkeypatch.setattr(network_discovery, "get_server_metadata", lambda sid: {})

    cfg = OmlxBackend()._cfg("ns-1")
    assert cfg == {
        "server_id": "ns-1",
        "host": "127.0.0.1",
        "port": 8000,
        "instance_name": "Studio",
        "api_key": None,
    }


def test_omlx_cfg_pulls_api_key_from_metadata(monkeypatch):
    from llm import network_discovery, network_endpoints

    monkeypatch.setattr(network_discovery, "get_server", lambda sid: {
        "id": sid, "instance_name": "Studio", "port": 8000,
    })
    monkeypatch.setattr(network_endpoints, "get_active_endpoint",
                        lambda sid: {"host": "127.0.0.1"})
    monkeypatch.setattr(network_discovery, "get_server_metadata",
                        lambda sid: {"api_key": "sk-stored"})

    cfg = OmlxBackend()._cfg("ns-1")
    assert cfg["api_key"] == "sk-stored"


def test_omlx_cfg_returns_id_only_when_row_missing(monkeypatch):
    from llm import network_discovery
    monkeypatch.setattr(network_discovery, "get_server", lambda sid: None)
    cfg = OmlxBackend()._cfg("missing")
    assert cfg == {"server_id": "missing"}


def test_omlx_check_health_routes_through_omlx_provider(monkeypatch):
    import asyncio
    from llm import network_discovery
    from llm.providers import omlx as omlx_provider

    monkeypatch.setattr(network_discovery, "get_server", lambda sid: {
        "id": sid, "instance_name": "Studio", "port": 8000,
    })

    captured: dict = {}

    async def fake_check_server(cfg):
        captured["cfg"] = cfg
        return {"running": True, "api_base": "http://omlx.test/v1"}

    async def loaded(api_base, model, *, server_id=None):
        captured["loaded"] = (api_base, model, server_id)

    monkeypatch.setattr(omlx_provider, "check_server", fake_check_server)
    monkeypatch.setattr(omlx_provider, "require_loaded_model", loaded)

    healthy, reason = asyncio.run(
        OmlxBackend().check_health("omlx@ns-1/qwen3.5-vl"),
    )
    assert healthy is True
    assert reason == ""
    assert captured["cfg"]["server_id"] == "ns-1"
    assert captured["loaded"] == ("http://omlx.test/v1", "qwen3.5-vl", "ns-1")


def test_omlx_check_health_surfaces_unreachable(monkeypatch):
    import asyncio
    from llm import network_discovery
    from llm.providers import omlx as omlx_provider

    monkeypatch.setattr(network_discovery, "get_server", lambda sid: {
        "id": sid, "instance_name": "Studio", "port": 8000,
    })

    async def fake_check_server(cfg):
        return {"running": False}

    monkeypatch.setattr(omlx_provider, "check_server", fake_check_server)

    healthy, reason = asyncio.run(
        OmlxBackend().check_health("omlx@ns-1/qwen3.5-vl"),
    )
    assert healthy is False
    assert "Studio" in reason
    assert "not reachable" in reason


def test_omlx_check_health_rejects_selected_unloaded_model(monkeypatch):
    import asyncio
    from llm import network_discovery, network_endpoints
    from llm.providers import omlx as omlx_provider

    monkeypatch.setattr(network_discovery, "get_server", lambda sid: {
        "id": sid, "instance_name": "Studio", "port": 8000,
    })

    async def fake_freshness(_server_id, *, budget_s=1.0):
        return True

    async def fake_check_server(_cfg):
        return {"running": True, "api_base": "http://omlx.test/v1"}

    async def unloaded(_api_base, _model, *, server_id=None):
        assert server_id == "ns-1"
        raise omlx_provider.OmlxModelNotLoadedError("model not loaded: qwen3.5-vl")

    monkeypatch.setattr(network_endpoints, "freshness_guard", fake_freshness)
    monkeypatch.setattr(omlx_provider, "check_server", fake_check_server)
    monkeypatch.setattr(omlx_provider, "require_loaded_model", unloaded)

    healthy, reason = asyncio.run(
        OmlxBackend().check_health("omlx@ns-1/qwen3.5-vl"),
    )

    assert healthy is False
    assert "not loaded" in reason.lower()
    assert "Studio" in reason


def test_omlx_check_health_invokes_freshness_guard(monkeypatch):
    import asyncio
    from llm import network_discovery, network_endpoints
    from llm.providers import omlx as omlx_provider

    monkeypatch.setattr(network_discovery, "get_server", lambda sid: {
        "id": sid, "instance_name": "Studio", "port": 8000,
    })

    freshness_calls: list[str] = []

    async def fake_freshness(server_id, *, budget_s=1.0):
        freshness_calls.append(server_id)
        return True

    monkeypatch.setattr(network_endpoints, "freshness_guard", fake_freshness)

    async def fake_check_server(_cfg):
        return {"running": True, "api_base": "http://omlx.test/v1"}

    async def loaded(_api_base, _model, *, server_id=None):
        assert server_id == "ns-1"

    monkeypatch.setattr(omlx_provider, "check_server", fake_check_server)
    monkeypatch.setattr(omlx_provider, "require_loaded_model", loaded)

    healthy, _ = asyncio.run(OmlxBackend().check_health("omlx@ns-1/qwen3.5-vl"))
    assert healthy is True
    assert freshness_calls == ["ns-1"]


def test_omlx_check_health_handles_missing_server(monkeypatch):
    import asyncio
    from llm import network_discovery
    monkeypatch.setattr(network_discovery, "get_server", lambda sid: None)

    healthy, reason = asyncio.run(
        OmlxBackend().check_health("omlx@gone/x"),
    )
    assert healthy is False
    assert "not found" in reason
