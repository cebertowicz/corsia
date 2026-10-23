"""Schema 146 -> 147: persisted Measurement boundary breaks."""
from __future__ import annotations

import hashlib
import sqlite3

from api.measurement_breaks import _SOURCE_START_SQL, _breaks_query, _source_group
from api.measurement_registry import REGISTRY
from core.measurement_token_contract import priceable_usage_predicate
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_146_to_147_measure_breaks import migrate_146_to_147
from infra.schema import SCHEMA_VERSION
from tests.plan_writer_authorizer import raw_plan_seed


def _breaks(db):
    return [tuple(row) for row in db.execute("SELECT period,kind,metric_group,reason FROM measure_breaks ORDER BY period,kind")]


def test_v147_fresh_schema_has_no_install_history_rows() -> None:
    assert SCHEMA_VERSION == 147
    assert MIGRATIONS[146] is migrate_146_to_147
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    assert _breaks(db) == []


def _combined_starts_plan(db: sqlite3.Connection) -> str:
    groups = tuple(dict.fromkeys(_source_group(metric) for metric in REGISTRY))
    known = tuple(group for group in groups if group in _SOURCE_START_SQL)
    query = _breaks_query((*known, "asks_hold"), include_models=False)
    return "\n".join(str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + query))


def test_v147_upgrade_leaves_a_history_free_install_empty() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    db.execute("DROP TABLE measure_breaks")
    db.execute("PRAGMA user_version=146")
    db.commit()
    assert migrate_to_current(db, SCHEMA_VERSION).to_version == 147
    assert _breaks(db) == []
    assert migrate_to_current(db, SCHEMA_VERSION).migrated is False
    migrate_146_to_147(db)
    assert _breaks(db) == []


def _migrate_with_outcome(created_at: str) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    db.execute("DROP TABLE measure_breaks")
    db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES ('plan','thread','thread','active',1,'t','t')")
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at) VALUES ('node','plan',0,'task','t','t','accepted','commitment',1,1,'t','t')")
        db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) VALUES ('accepted','node','test','accepted','active','accepted',?)", (created_at,))
    db.commit()
    migrate_146_to_147(db)
    return db


def test_v147_upgrade_seeds_world_mark_only_when_own_outcomes_predate_it() -> None:
    before = _migrate_with_outcome("2026-08-30T23:59:59Z")
    after = _migrate_with_outcome("2026-08-31T00:00:00Z")
    try:
        assert ("2026-08-31", "world", None, "plan nodes got about 5 times smaller") in _breaks(before)
        assert ("2026-08-31", "world", None, "plan nodes got about 5 times smaller") not in _breaks(after)
    finally:
        before.close()
        after.close()


def test_v147_upgrade_seeds_token_mark_only_when_own_usage_predates_it() -> None:
    def migrated(token_created_at: str) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        initialize_schema(db)
        db.execute("DROP TABLE measure_breaks")
        db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at) VALUES ('usage','thread','m',1,0,0,0,1,'test',?)", (token_created_at,))
        db.commit()
        migrate_146_to_147(db)
        return db

    before = migrated("2026-09-06T23:59:59Z")
    after = migrated("2026-09-07T00:00:00Z")
    try:
        token_mark = ("2026-09-07", "source", "tokens", "token totals cover every harness from this week")
        assert token_mark in _breaks(before)
        assert token_mark not in _breaks(after)
    finally:
        before.close()
        after.close()


def test_v147_migration_keeps_the_combined_start_read_on_its_contract_indexes() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    for name in (
        "idx_llm_usage_priceable_measurement_start", "idx_measurement_user_holds_start",
        "idx_measurement_rejections_time", "idx_measurement_decisions_raised",
        "idx_measurement_decisions_answered", "idx_measurement_first_try_failed",
        "idx_measurement_first_try_corrections", "idx_measurement_first_try_released_bad",
        "idx_measurement_size_rater",
    ):
        db.execute(f"DROP INDEX {name}")
    db.execute("DROP TABLE measure_breaks")
    db.execute("PRAGMA user_version=146")
    db.commit()

    assert migrate_to_current(db, SCHEMA_VERSION).to_version == 147
    plan = _combined_starts_plan(db)
    for index in (
        "idx_llm_usage_priceable_measurement_start", "idx_measurement_user_holds_start",
        "idx_measurement_decisions_raised", "idx_measurement_decisions_answered",
        "idx_measurement_first_try_failed", "idx_measurement_first_try_corrections",
        "idx_measurement_first_try_released_bad", "idx_measurement_size_rater",
        "idx_plan_node_events_created",
    ):
        assert index in plan
    for table in ("plan_node_events", "llm_usage", "outcome_events", "presence_visits", "artifact_identities"):
        assert f"SCAN {table}" not in plan
    assert "USE TEMP B-TREE" not in plan


def test_v147_upgrade_skips_marks_and_starts_whose_legacy_source_tables_are_absent() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    migrate_146_to_147(db)
    assert _breaks(db) == []


def test_priceable_contract_change_needs_an_index_rebuild_migration() -> None:
    digest = hashlib.sha256(priceable_usage_predicate().encode()).hexdigest()[:16]
    assert digest == "1fc012f1036ffec4"
