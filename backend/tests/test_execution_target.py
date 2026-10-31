from __future__ import annotations

import json
import sqlite3

import pytest

from vendor_account_fixtures import signed_in_default


@pytest.fixture
def target_db():
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    yield db
    db.close()


def _insert_model(db, *, model_id: str, harness: str, provider: str,
                  selector: str, transport: str, backend: str,
                  available: bool = True, context_window: int | None = 123456,
                  display_name: str | None = None,
                  supports_vision: bool = False,
                  supports_reasoning: bool = False,
                  metadata: dict | None = None):
    db.execute(
        "INSERT INTO models "
        "(id,harness,provider,provider_model_id,litellm_model,display_name,"
        "backend,source,enabled,available,context_window,supports_vision,"
        "supports_reasoning,metadata_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            model_id, harness, provider, selector, transport, display_name or model_id,
            backend, "test", 1, int(available), context_window,
            int(supports_vision), int(supports_reasoning),
            json.dumps(metadata or {}), "t", "t",
        ),
    )
    db.commit()


def _connection(db, provider: str, status: str = "available"):
    db.execute(
        "INSERT INTO connections "
        "(id,type,provider,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (provider, "cli", provider, status, "t", "t"),
    )
    db.commit()


def test_native_target_uses_catalog_transport_not_id_shape(target_db):
    from llm.resolution import ExecutionTarget, resolve_execution_target

    _insert_model(
        target_db, model_id="codex/misleading", harness="native",
        provider="openai", selector="gpt-real", transport="openai/gpt-real",
        backend="api",
    )

    target = resolve_execution_target("codex/misleading", db=target_db)
    assert target == ExecutionTarget(
        catalog_id="codex/misleading", harness="native", provider="openai",
        backend="api", transport_model="openai/gpt-real",
        expected_reported_model=None, context_window=123456,
        available=True, availability_reason=None,
        display_name="codex/misleading", supports_reasoning=False,
        supports_vision=False, discovered_context_window=123456,
    )


def test_harness_target_preserves_exact_selector_identity_and_context(target_db):
    from llm.resolution import resolve_execution_target

    _connection(target_db, "claude-code")
    _insert_model(
        target_db, model_id="claude-code/claude-fable-5[1m]",
        harness="claude-code", provider="claude-code",
        selector="claude-fable-5[1m]",
        transport="claude-code/claude-fable-5[1m]", backend="cc",
        context_window=967000,
        display_name="Fable 5", supports_vision=True, supports_reasoning=True,
        metadata={
            "resolved_identity": "claude-fable-5",
            # Reasoning support is what discovery reports: the model's effort levels.
            "discovered_capabilities": {"effort_levels": ["low", "high"]},
        },
    )
    signed_in_default("claude-code", db=target_db)

    target = resolve_execution_target(
        "claude-code/claude-fable-5[1m]", db=target_db
    )
    assert target.harness == "claude-code"
    assert target.transport_model == "claude-fable-5[1m]"
    assert target.expected_reported_model == "claude-fable-5"
    assert target.context_window == 967000
    assert target.display_name == "Fable 5"
    assert target.supports_vision is True
    assert target.supports_reasoning is True
    assert target.available is True


def test_live_connection_state_is_part_of_effective_harness_availability(target_db):
    from llm.resolution import resolve_execution_target

    _connection(target_db, "codex", "needs_auth")
    _insert_model(
        target_db, model_id="codex/gpt-5.6-sol", harness="codex",
        provider="codex", selector="gpt-5.6-sol",
        transport="codex/gpt-5.6-sol", backend="codex",
    )

    target = resolve_execution_target("codex/gpt-5.6-sol", db=target_db)
    assert target.available is False
    assert target.availability_reason == "connection_unavailable"


def test_invalid_harness_and_ambiguous_transport_fail_loud(target_db):
    from llm.resolution import resolve_execution_target

    _insert_model(
        target_db, model_id="bad", harness="native", provider="one",
        selector="one", transport="shared/transport", backend="api",
    )
    _insert_model(
        target_db, model_id="bad-2", harness="native", provider="two",
        selector="two", transport="shared/transport", backend="api",
    )
    target_db.execute("UPDATE models SET harness='surprise' WHERE id='bad'")
    target_db.commit()

    with pytest.raises(ValueError, match="unsupported harness"):
        resolve_execution_target("bad", db=target_db)
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_execution_target("shared/transport", db=target_db)


def test_missing_selection_fails_before_a_target_is_constructed(target_db):
    from llm.resolution import ModelUnavailableError, resolve_execution_target

    with pytest.raises(ModelUnavailableError, match="not found in catalog"):
        resolve_execution_target("codex/not-real", db=target_db)


@pytest.mark.parametrize(
    ("metadata_value", "expected"),
    [(True, True), (False, False), ("true", None), (None, None)],
)
def test_native_target_carries_boolean_preserve_thinking_default_only(
    target_db, metadata_value, expected,
):
    from llm.resolution import resolve_execution_target

    metadata = {}
    if metadata_value is not None:
        metadata["preserve_thinking_default"] = metadata_value
    _insert_model(
        target_db,
        model_id="managed-omlx",
        harness="native",
        provider="omlx",
        selector="ReasoningModel",
        transport="omlx@srv-test/ReasoningModel",
        backend="network",
        metadata=metadata,
    )

    target = resolve_execution_target("managed-omlx", db=target_db)

    assert target.preserve_thinking_default is expected
