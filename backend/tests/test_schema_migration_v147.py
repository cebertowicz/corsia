"""Schema 146 -> 147: immutable legacy Measurement boundary facts."""
from __future__ import annotations

import sqlite3

import pytest

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current
from infra.migration_146_to_147_measure_breaks import migrate_146_to_147
from infra.schema import SCHEMA_VERSION
from tests.plan_writer_authorizer import raw_plan_seed


def _breaks(db):
    return [tuple(row) for row in db.execute(
        "SELECT period,kind,metric_group,reason FROM measure_breaks ORDER BY period,kind,metric_group"
    )]


def _legacy_db() -> sqlite3.Connection:
    # Startup migrations receive sqlite's default positional rows.
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    db.execute("DROP TABLE measure_breaks")
    db.execute("PRAGMA user_version=146")
    return db


def _accepted(db, when: str) -> None:
    db.execute("INSERT INTO users (id,password_hash,created_at,password_changed_at) VALUES ('steve','test','t','t')")
    db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('thread','steve','t','t','t')")
    db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES ('plan','thread','thread','active',1,'t','t')")
    with raw_plan_seed(db):
        db.execute(
            "INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at) "
            "VALUES ('node','plan',0,'task','t','t','accepted','commitment',1,1,'t','t')"
        )
        db.execute(
            "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
            "VALUES ('accepted','node','test','accepted','active','accepted',?)", (when,)
        )


def test_v147_fresh_schema_has_no_install_history_rows() -> None:
    assert SCHEMA_VERSION == 165
    assert MIGRATIONS[146] is migrate_146_to_147
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    assert _breaks(db) == []


def test_v147_failure_after_the_step_rolls_back_every_v147_object(monkeypatch) -> None:
    def failing_step(db):
        migrate_146_to_147(db)
        raise RuntimeError("synthetic failure after the v147 step")

    monkeypatch.setitem(MIGRATIONS, 146, failing_step)
    db = _legacy_db()
    try:
        _accepted(db, "2026-08-10T12:00:00Z")
        db.commit()
        before = list(db.iterdump())
        with pytest.raises(MigrationError, match="synthetic failure after the v147 step"):
            migrate_to_current(db, SCHEMA_VERSION)
        assert not db.in_transaction
        assert db.execute("PRAGMA user_version").fetchone()[0] == 146
        assert list(db.iterdump()) == before
    finally:
        db.close()


def test_v147_upgrade_leaves_a_history_free_install_empty() -> None:
    db = _legacy_db()
    try:
        assert migrate_to_current(db, SCHEMA_VERSION).to_version == 165
        assert _breaks(db) == []
        assert migrate_to_current(db, SCHEMA_VERSION).migrated is False
    finally:
        db.close()


def test_v147_migration_seeds_legacy_first_try_at_latest_required_source() -> None:
    db = _legacy_db()
    try:
        _accepted(db, "2026-08-10T12:00:00Z")
        db.execute(
            "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
            "VALUES ('rejected','node','test','review_rejected','active','active','2026-09-14T12:00:00Z')"
        )
        db.commit()
        migrate_146_to_147(db)
        assert ("2026-09-14T12:00:00Z", "start", "first_try", "first recorded on this computer") in _breaks(db)
    finally:
        db.close()


def test_v147_migration_seeds_legacy_asks_at_user_hold_boundary() -> None:
    db = _legacy_db()
    try:
        _accepted(db, "2026-08-10T12:00:00Z")
        db.execute(
            "INSERT INTO artifact_identities (id,integration,type,external_id,metadata,created_at,updated_at) "
            "VALUES ('decision','local','plan-decision','1','{\"plan_decision\":{\"raised_at\":\"2026-09-01T12:00:00Z\"}}','t','t')"
        )
        db.execute(
            "INSERT INTO plan_node_holds (id,node_id,kind,reason_key,reason,owner_kind,provenance,created_at) "
            "VALUES ('hold','node','blocked','test','test','user','test','2026-09-07T12:00:00Z')"
        )
        db.commit()
        migrate_146_to_147(db)
        assert ("2026-09-07T12:00:00Z", "start", "asks_per_outcome", "first recorded on this computer") in _breaks(db)
    finally:
        db.close()


def test_v147_migration_keeps_only_legacy_marks_supported_by_history() -> None:
    db = _legacy_db()
    try:
        _accepted(db, "2026-08-30T23:59:59Z")
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at) "
            "VALUES ('usage','thread','m',1,0,0,0,1,'test','2026-09-06T23:59:59Z')"
        )
        db.commit()
        migrate_146_to_147(db)
        assert ("2026-08-31", "world", None, "tasks were split about 5 times finer from Aug 31, 2026 on") in _breaks(db)
        assert ("2026-09-07", "source", "tokens", "token totals cover every agent from Sep 7, 2026 on") in _breaks(db)
    finally:
        db.close()


def test_v147_upgrade_skips_starts_when_legacy_source_tables_are_absent() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        migrate_146_to_147(db)
        assert _breaks(db) == []
    finally:
        db.close()


def _shape(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='measure_breaks' ORDER BY name")
    return [(kind, name, " ".join((sql or "").split())) for kind, name, sql in rows]


def test_v147_upgrade_matches_today_fresh_install() -> None:
    fresh = sqlite3.connect(":memory:")
    db = _legacy_db()
    try:
        initialize_schema(fresh)
        migrate_to_current(db, SCHEMA_VERSION)
        assert _shape(db) == _shape(fresh)
    finally:
        db.close()
        fresh.close()


def test_v147_step_keeps_its_shape_when_the_live_definition_changes(monkeypatch) -> None:
    import importlib

    import infra.measurement_schema as live
    import infra.migration_146_to_147_measure_breaks as step

    # Reloading under a changed live definition is what a later schema version looks like to this step.
    monkeypatch.setattr(live, "MEASUREMENT_BREAKS_SCHEMA_SQL", "CREATE TABLE measure_breaks (period TEXT, later TEXT);")
    db = sqlite3.connect(":memory:")
    try:
        importlib.reload(step).migrate_146_to_147(db)
        columns = [row[1] for row in db.execute("PRAGMA table_info(measure_breaks)")]
        indexes = {row[1] for row in db.execute("PRAGMA index_list(measure_breaks)")}
        assert columns == ["period", "kind", "metric_group", "reason"]
        assert {"idx_measure_breaks_identity", "idx_measure_break_starts_group"} <= indexes
    finally:
        db.close()
        monkeypatch.undo()
        importlib.reload(step)
