from __future__ import annotations

import pytest


def _catalog_row(**over):
    row = {
        "id": "cat-x",
        "harness": "native",
        "backend": "network",
        "provider": "omlx",
        "display_name": "Model X",
        "description": "",
        "litellm_model": "omlx@srv-local/ModelX",
        "context_window": 32768,
        "provider_model_id": "ModelX",
        "supports_vision": False,
        "supports_tools": True,
        "metadata_json": '{"server_name": "This Mac"}',
        "available": True,
    }
    row.update(over)
    return row


@pytest.fixture
def status_env(monkeypatch):
    """Wire the accessor's external inputs to controllable fakes."""
    from llm import catalog as model_catalog
    import llm.model_status as ms

    servers = {"srv-local": "online"}
    loaded = {"srv-local": set()}
    rows: list[dict] = []

    monkeypatch.setattr(model_catalog, "get_model_status_catalog_rows", lambda: rows)
    monkeypatch.setattr(ms, "_network_server_status", lambda: dict(servers))
    monkeypatch.setattr(ms, "_loaded_model_ids_fn", lambda: (lambda sid: loaded.get(sid, set())))
    monkeypatch.setattr(ms, "_cloud_providers_active", lambda: {"anthropic"})
    harness_connections = {"claude-code": (False, True), "codex": (False, True)}
    envelopes: dict[str, dict | None] = {}
    monkeypatch.setattr(
        ms, "_harness_connection_facts",
        lambda: dict(harness_connections),
    )
    monkeypatch.setattr(
        ms, "_harness_discovery_envelope", lambda provider: envelopes.get(provider),
        raising=False,
    )

    class Env:
        def set_rows(self, r):
            rows[:] = r

        def set_loaded(self, sid, ids):
            loaded[sid] = set(ids)

        def set_server(self, sid, st):
            servers[sid] = st

        def set_harness_connection(self, provider, available, enabled=True):
            harness_connections[provider] = (available, enabled)

        def set_envelope(self, provider, envelope):
            envelopes[provider] = envelope

    return Env()


class TestGetModelStatusRows:
    def test_network_ready_when_loaded(self, status_env):
        from llm.model_status import get_model_status_rows
        status_env.set_rows([_catalog_row()])
        status_env.set_loaded("srv-local", {"ModelX"})
        row = next(r for r in get_model_status_rows() if r["id"] == "cat-x")
        assert row["status"] == "ready"
        assert row["selector"] == "omlx@srv-local/ModelX"
        assert row["connection"] == "omlx"
        assert row["context_window"] == 32768
        assert row["litellm_model"] == "omlx@srv-local/ModelX"
        assert row["server_name"] == "This Mac"

    def test_network_available_when_online_not_loaded(self, status_env):
        # The dead-spawn trap state: server online, model NOT loaded.
        from llm.model_status import get_model_status_rows
        status_env.set_rows([_catalog_row()])  # loaded empty
        row = next(r for r in get_model_status_rows() if r["id"] == "cat-x")
        assert row["status"] == "available"

    def test_network_unavailable_when_server_offline(self, status_env):
        from llm.model_status import get_model_status_rows
        status_env.set_rows([_catalog_row()])
        status_env.set_server("srv-local", "offline")
        row = next(r for r in get_model_status_rows() if r["id"] == "cat-x")
        assert row["status"] == "unavailable"

    def test_missing_context_window_is_unavailable(self, status_env):
        from llm.model_status import get_model_status_rows
        status_env.set_rows([_catalog_row(context_window=None)])
        status_env.set_loaded("srv-local", {"ModelX"})
        row = next(r for r in get_model_status_rows() if r["id"] == "cat-x")
        assert row["status"] == "unavailable"
        assert row["status_reason"] == "missing_context_window"

    def test_cloud_ready_only_when_provider_active(self, status_env):
        from llm.model_status import get_model_status_rows
        api_row = _catalog_row(
            id="gpt", backend="api", provider="openai",
            litellm_model="openai/gpt-5.5", display_name="GPT-5.5",
            metadata_json="{}",
        )
        status_env.set_rows([api_row])
        # only anthropic active -> openai row filtered out entirely
        assert not [r for r in get_model_status_rows() if r["id"] == "gpt"]

    def test_harness_rows_project_directly_from_canonical_catalog(self, status_env):
        from llm.model_status import get_model_status_rows
        status_env.set_harness_connection("codex", True)
        status_env.set_rows([_catalog_row(
            id="codex/spark-probe", harness="codex", backend="codex",
            provider="codex", provider_model_id="spark-probe",
            display_name="Spark", litellm_model="codex/spark-probe",
            context_window=128000,
            metadata_json='{"discovered_capabilities": {}, "discovery_order": 0}',
        )])
        codex = [r for r in get_model_status_rows() if r["backend"] == "codex"]
        assert len(codex) == 1
        assert codex[0]["id"] == "codex/spark-probe"
        assert codex[0]["name"] == "Codex · Spark"
        assert codex[0]["display_name"] == "Spark"
        assert codex[0]["available"] is True
        assert codex[0]["status"] == "ready"
        assert codex[0]["capabilities"]["supports_vision"] is True

    @pytest.mark.parametrize(("identity", "expected"), [
        ("claude-opus-5-5", "Opus 5.5"),
        ("claude-sonnet-5-5", "Sonnet 5.5"),
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
        ("claude-opus-5-5[1m]", "Opus 5.5"),
    ])
    def test_claude_display_name_is_projected_from_resolved_identity(self, status_env, identity, expected):
        from llm.model_status import get_model_status_rows
        status_env.set_harness_connection("claude-code", True)
        status_env.set_rows([_catalog_row(
            id="claude-code/sonnet", harness="claude-code", backend="claude-code",
            provider="claude-code", provider_model_id="sonnet", display_name="Sonnet",
            litellm_model="claude-code/sonnet", context_window=200000,
            metadata_json=f'{{"resolved_identity": "{identity}"}}',
        )])

        row = get_model_status_rows()[0]

        assert row["display_name"] == expected
        assert row["name"] == f"Claude Code · {expected}"

    def test_a_persons_label_wins_over_the_versioned_name(self, status_env):
        from llm.model_status import get_model_status_rows
        status_env.set_harness_connection("claude-code", True)
        status_env.set_rows([_catalog_row(
            id="claude-code/sonnet", harness="claude-code", backend="claude-code",
            provider="claude-code", provider_model_id="sonnet", display_name="Sonnet",
            alias="My Sonnet", litellm_model="claude-code/sonnet", context_window=200000,
            metadata_json='{"resolved_identity": "claude-sonnet-5-5"}',
        )])

        assert get_model_status_rows()[0]["display_name"] == "My Sonnet"

    def test_harness_connection_and_envelope_override_public_status(self, status_env):
        from llm.model_status import get_model_status_rows
        status_env.set_rows([_catalog_row(
            id="claude-code/sonnet", harness="claude-code", backend="cc",
            provider="claude-code", provider_model_id="sonnet",
            display_name="Sonnet", litellm_model="claude-code/sonnet",
            context_window=967000,
            metadata_json='{"discovered_capabilities": {}, "discovery_order": 0}',
        )])
        disconnected = get_model_status_rows()[0]
        assert disconnected["status"] == "unavailable"
        assert disconnected["status_reason"] == "connection_unavailable"

        status_env.set_harness_connection("claude-code", True)
        status_env.set_envelope("claude-code", {"last_error": "cached failure"})
        cached = get_model_status_rows()[0]
        assert cached["status"] == "ready"
        assert cached["cache_marker"] == "Cached"

    def test_disabled_harness_stays_in_catalog_with_its_own_reason(self, status_env):
        from llm.model_status import get_model_status_rows
        status_env.set_harness_connection("codex", True, enabled=False)
        status_env.set_rows([_catalog_row(
            id="codex/spark-probe", harness="codex", backend="codex",
            provider="codex", provider_model_id="spark-probe", display_name="Spark",
            litellm_model="codex/spark-probe", context_window=128000,
            metadata_json='{"discovered_capabilities": {}, "discovery_order": 0}',
        )])

        row = get_model_status_rows()[0]

        assert row["status"] == "unavailable"
        assert row["status_reason"] == "provider_disabled"

    def test_harness_without_connection_row_is_not_reported_as_turned_off(self, status_env, monkeypatch):
        from llm import model_status as ms
        from llm.model_status import get_model_status_rows
        monkeypatch.setattr(ms, "_harness_connection_facts", lambda: {})
        status_env.set_rows([_catalog_row(
            id="codex/spark-probe", harness="codex", backend="codex",
            provider="codex", provider_model_id="spark-probe", display_name="Spark",
            litellm_model="codex/spark-probe", context_window=128000,
            metadata_json='{"discovered_capabilities": {}, "discovery_order": 0}',
        )])

        row = get_model_status_rows()[0]

        assert row["status"] == "unavailable"
        assert row["status_reason"] == "connection_unavailable"

    def test_user_alias_overrides_display_name_in_switcher(self, status_env):
        # A user-set alias replaces the discovered display_name in the picker.
        from llm.model_status import get_model_status_rows
        status_env.set_rows([_catalog_row(alias="My Local Model")])
        status_env.set_loaded("srv-local", {"ModelX"})
        row = next(r for r in get_model_status_rows() if r["id"] == "cat-x")
        assert row["name"] == "My Local Model"
        assert row["display_name"] == "My Local Model"

    def test_user_alias_overrides_display_name_under_harness_prefix(self, status_env):
        # The alias replaces the model component; the harness prefix stays.
        from llm.model_status import get_model_status_rows
        status_env.set_harness_connection("codex", True)
        status_env.set_rows([_catalog_row(
            id="codex/spark-probe", harness="codex", backend="codex",
            provider="codex", provider_model_id="spark-probe",
            display_name="Spark", alias="Sparky",
            litellm_model="codex/spark-probe", context_window=128000,
            metadata_json='{"discovered_capabilities": {}, "discovery_order": 0}',
        )])
        codex = next(r for r in get_model_status_rows() if r["backend"] == "codex")
        assert codex["name"] == "Codex · Sparky"
        assert codex["display_name"] == "Sparky"
