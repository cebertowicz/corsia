"""AC-P2-0 — E5 key-leak GATE (hard, blocks the external add-flow).

The threat: an External (user-owned) provider is a bare base-URL + optional
token that CORSIA dispatches to. For the anthropic dialect CORSIA uses the
native ``AsyncAnthropic`` SDK, which — when constructed WITHOUT an explicit
api_key — silently falls back to ``os.environ['ANTHROPIC_API_KEY']`` (populated
from CORSIA's OWN global cloud credential). A keyless external endpoint would
then receive CORSIA's real Anthropic key. That is the leak E5 forecloses.

The structural fix (llm/external_provider.build_external_params): a keyless
external provider dispatches with a NON-EMPTY sentinel (``sk-noauth``), and
llm/providers/anthropic._get_client passes api_key through to the SDK whenever
it is truthy — so the env fallback can never fire for an external call.

These tests are the GATE's mandated **payload-level** proof: they run the real
chain (params → real _get_client → real AsyncAnthropic instance) WITH the global
key present in the environment, and assert the outbound client carries the
sentinel and NOT the global key. Until this passes, the add-flow does not ship.
"""

from __future__ import annotations

import pytest

GLOBAL_KEY = "sk-ant-CORSIA-GLOBAL-DO-NOT-LEAK"


@pytest.fixture
def global_anthropic_key(monkeypatch):
    """Simulate CORSIA's own global Anthropic credential in the environment —
    the exact condition under which a keyless external call could leak it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", GLOBAL_KEY)
    return GLOBAL_KEY


def _wire_external_server(monkeypatch, *, dialect: str, api_key):
    """Stand in for a freshly added external provider row (the 'real add-flow')."""
    from llm import network_discovery, network_endpoints

    server = {"id": "ns-ext", "dialect": dialect, "port": 8080,
              "instance_name": "my-box", "status": "online"}
    monkeypatch.setattr(network_discovery, "get_server", lambda sid: server)
    monkeypatch.setattr("llm.external_provider.resolve_server", lambda sid: api_key)
    monkeypatch.setattr(network_endpoints, "get_active_endpoint",
                        lambda sid: {"host": "10.0.0.9"})
    # The one URL owner; both dispatch legs derive their base from it.
    monkeypatch.setattr(network_endpoints, "build_api_base",
                        lambda sid: "http://10.0.0.9:8080/v1")


def _anthropic_client_for(params):
    """Construct the real AsyncAnthropic client the dispatch leg would build."""
    from llm.providers import anthropic as anthropic_leg
    return anthropic_leg._get_client(params.get("api_key"), params.get("api_base"))


class TestKeyLeakGate:
    def test_keyless_anthropic_external_does_not_leak_global_key(
        self, monkeypatch, global_anthropic_key
    ):
        """The gate. Keyless anthropic external + global key present → the real
        outbound client carries the sentinel, NOT CORSIA's global key."""
        from llm.external_provider import build_external_params

        _wire_external_server(monkeypatch, dialect="anthropic", api_key=None)
        params = build_external_params("ns-ext", "claude-x")

        assert params["api_key"] == "sk-noauth"           # sentinel, non-empty
        assert GLOBAL_KEY not in params.values()          # global absent from params

        client = _anthropic_client_for(params)
        # Payload-level: the SDK's RESOLVED key (env fallback included) is the
        # sentinel — the global key never reaches the external endpoint.
        assert client.api_key == "sk-noauth"
        assert client.api_key != GLOBAL_KEY
        assert str(client.base_url).startswith("http://10.0.0.9:8080")

    def test_guard_is_load_bearing_keyless_client_WOULD_leak(
        self, global_anthropic_key
    ):
        """Negative control: prove the guard actually matters. A keyless SDK
        client (no sentinel) DOES resolve to the global key — so the sentinel is
        load-bearing, not decorative."""
        import anthropic

        leaky = anthropic.AsyncAnthropic()  # the pre-fix behaviour
        assert leaky.api_key == GLOBAL_KEY  # confirms the threat is real

    def test_user_supplied_token_is_used_not_global(
        self, monkeypatch, global_anthropic_key
    ):
        """A user's OWN token flows to their endpoint; CORSIA's global is never
        substituted or blended in."""
        from llm.external_provider import build_external_params

        _wire_external_server(monkeypatch, dialect="anthropic", api_key="sk-user-own")
        params = build_external_params("ns-ext", "claude-x")
        assert params["api_key"] == "sk-user-own"

        client = _anthropic_client_for(params)
        assert client.api_key == "sk-user-own"
        assert client.api_key != GLOBAL_KEY

    def test_keyless_openai_external_does_not_leak_openai_global(
        self, monkeypatch
    ):
        """Same closure on the openai leg: keyless openai external uses the
        sentinel, never OPENAI_API_KEY."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-GLOBAL-DO-NOT-LEAK")
        from llm.external_provider import build_external_params

        _wire_external_server(monkeypatch, dialect="openai", api_key=None)
        params = build_external_params("ns-ext", "qwen")

        assert params["api_key"] == "sk-noauth"
        assert "sk-openai-GLOBAL-DO-NOT-LEAK" not in params.values()
        assert params["api_base"] == "http://10.0.0.9:8080/v1"
