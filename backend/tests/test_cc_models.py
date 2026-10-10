from __future__ import annotations

import json
import sqlite3

import pytest


@pytest.fixture
def cc_db():
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.executemany(
        "INSERT INTO models "
        "(id,harness,provider,provider_model_id,litellm_model,display_name,"
        "backend,source,context_window,metadata_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "claude-code/sonnet", "claude-code", "claude-code", "sonnet",
                "claude-code/sonnet", "Sonnet", "cc", "test", 967000,
                json.dumps({"resolved_identity": "claude-sonnet-5"}), "t", "t",
            ),
            (
                "claude-code/fable", "claude-code", "claude-code", "fable",
                "claude-code/fable", "Fable", "cc", "test", None,
                json.dumps({"resolved_identity": "claude-fable-5"}), "t", "t",
            ),
        ],
    )
    db.execute(
        "INSERT INTO connections "
        "(id,type,provider,status,created_at,updated_at) "
        "VALUES ('cc','cli','claude-code','available','t','t')"
    )
    db.commit()
    yield db
    db.close()


def test_exact_target_carries_observed_window_and_reported_identity(cc_db):
    from llm.resolution import resolve_execution_target

    target = resolve_execution_target("claude-code/sonnet", db=cc_db)
    assert target.transport_model == "sonnet"
    assert target.expected_reported_model == "claude-sonnet-5"
    # Nothing saved: CORSIA checkpoints at its default, the provider's window travels with it.
    assert target.windows == (262_144, 967000, "default")


def test_exact_target_does_not_invent_missing_window(cc_db):
    from llm.resolution import resolve_execution_target

    target = resolve_execution_target("claude-code/fable", db=cc_db)
    assert target.expected_reported_model == "claude-fable-5"
    assert target.context_window is None


def test_exact_transport_selector_resolves_same_target(cc_db):
    from llm.resolution import resolve_harness_selector

    target = resolve_harness_selector("claude-code", "sonnet", db=cc_db)
    assert target.catalog_id == "claude-code/sonnet"
    assert target.windows == (262_144, 967000, "default")
