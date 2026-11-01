
"""Schema v78 gives existing databases the Measurement indexes fresh installs already have.

The claim worth testing is the one a single shared definition makes easy and a
copy makes silent: a database that reaches v78 through migration 77 -> 78
carries exactly the indexes a database created by the fresh schema carries. Each
test therefore measures both databases and compares them, rather than trusting
hand-copied SQL — and pins the stored definitions so a renamed or dropped index
cannot pass quietly.
"""


from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current

from infra.migration_77_to_78_measurement_query_indexes import migrate_77_to_78
from infra.schema import SCHEMA_VERSION

#: sqlite records an index's own statement text, minus its ``IF NOT EXISTS``,
#: and otherwise verbatim; each value here is one flattened line of that text.
#: The four serve the dashboard's queries: the scope's thread-tree walk, user
#: messages by time, review rejections by time, and plan-decision scope over
#: artifact identities.
_EXPECTED_INDEXES = {
    "idx_measurement_thread_children": (
        "CREATE INDEX idx_measurement_thread_children "
        "ON threads(parent_thread_id, user_id) WHERE parent_thread_id IS NOT NULL"
    ),
    "idx_measurement_human_messages_time": (
        "CREATE INDEX idx_measurement_human_messages_time "
        "ON messages(thread_id, created_at, id) WHERE role='user'"
    ),
    "idx_measurement_rejections_time": (
        "CREATE INDEX idx_measurement_rejections_time "
        "ON plan_node_events(created_at, node_id) WHERE event='review_rejected'"
    ),
    "idx_measurement_decisions_thread": (
        "CREATE INDEX idx_measurement_decisions_thread "
        "ON artifact_identities(json_extract(metadata, '$.plan_decision.thread_id'))"
        " WHERE integration='local' AND type='plan-decision' AND json_valid(metadata)"
    ),
}


def _measurement_indexes(db: sqlite3.Connection) -> dict[str, str]:
    """The v78 Measurement indexes this database carries, name -> flattened SQL.

    Filtered in Python: SQL ``LIKE 'idx_measurement_%'`` would treat ``_`` as a
    single-character wildcard and could admit an unrelated index.
    """
    return {
        str(name): " ".join((sql or "").split())
        for name, sql in db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index'"
        )
        if str(name).startswith("idx_measurement_")
    }


def _all_measurement_indexes(db: sqlite3.Connection) -> dict[str, str]:
    """Every current Measurement boundary and first-row index this database carries."""
    names = {"idx_llm_usage_priceable_measurement_start", "idx_measure_breaks_identity", "idx_measure_break_starts_group"}
    return {
        str(name): " ".join((sql or "").split())
        for name, sql in db.execute("SELECT name, sql FROM sqlite_master WHERE type='index'")
        if str(name).startswith("idx_measurement_") or str(name) in names
    }


def _fresh_indexes() -> dict[str, str]:
    """What a database created by the current schema carries."""
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        return _measurement_indexes(db)


def _fresh_all_indexes() -> dict[str, str]:
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        return _all_measurement_indexes(db)


def _build_existing_v77_database(path) -> None:
    """Write a v77 database: current tables, live rows, no Measurement indexes."""
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,next_message_seq,"
            "created_at,updated_at) VALUES ('t-1','u','T','m',2,'t0','t0')"
        )
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES ('m-1','t-1','user','body','2026-09-19T03:00:00',1)"
        )
        for name in _all_measurement_indexes(db):
            db.execute(f"DROP INDEX {name}")
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=77")
        db.commit()
        # Without this the upgrade below would prove nothing: a database that
        # already had the indexes passes whatever migration creates them.
        assert _measurement_indexes(db) == {}


def test_the_step_is_registered_at_77_and_81_is_current() -> None:
    assert SCHEMA_VERSION == 168
    assert MIGRATIONS[77] is migrate_77_to_78


def test_fresh_install_retains_the_v78_measurement_indexes() -> None:
    assert {name: _fresh_indexes()[name] for name in _EXPECTED_INDEXES} == _EXPECTED_INDEXES


def _plan(db: sqlite3.Connection, sql: str, params: dict) -> list[str]:
    return [str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + sql, params)]



def test_scope_walk_and_presence_query_search_their_indexes_without_planner_stats() -> None:
    """Production databases never run ANALYZE, so the plans must hold with no stats.

    The scope walk needs its child index; the presence query must seek both the
    owner's threads and their visits, rather than scan either base event table.
    """
    from api.measurement_presence import active_family_sql as _active_family_sql
    from api.measurement_queries import PREPARED_OUTCOMES, PREPARED_SCOPE
    from api.measurement_scope import query_params as _query_params, resolve_grain as _resolve_grain, scope_cte as _scope_cte

    # The active-hours SQL buckets by the page grain, so its binds come from one.
    grain = _resolve_grain("weeks", "UTC", now=datetime(2026, 9, 28, 12, tzinfo=timezone.utc))
    params = _query_params({"user_id": "u", "scope_project": "all", "scope_agent": None, "grain": grain,
                            "from": "2026-09-07", "to": "2026-09-21"})
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_stat1'"
        ).fetchone()[0] == 0
        db.execute("CREATE TEMP TABLE measurement_scope_threads (id TEXT PRIMARY KEY)")
        db.execute("CREATE TEMP TABLE measurement_outcome_periods (period TEXT PRIMARY KEY, outcomes INTEGER)")

        scope = _plan(db, _scope_cte() + "SELECT id FROM scope_threads", params)
        recursive = scope[scope.index("RECURSIVE STEP") + 1:]
        assert any("SEARCH child USING INDEX idx_measurement_thread_children" in step for step in recursive), scope

        presence = _plan(db, _active_family_sql(grain, PREPARED_SCOPE, PREPARED_OUTCOMES), params)
        # Thread-owned visits start from the viewer's indexed threads, then seek
        # each thread's visits. Project-only visits use the same index's NULL key.
        assert any("SEARCH t USING INDEX idx_threads_user (user_id=?)" in step for step in presence), presence
        assert any("SEARCH v USING INDEX idx_presence_visits_thread (thread_id=? AND entered_at<?)" in step
                   for step in presence), presence
        assert not any("SCAN presence_visits" in step or "SCAN messages" in step for step in presence), presence


def test_scope_walk_keeps_its_child_search_when_analyzed_with_only_root_threads() -> None:
    """Root threads' NULL parents must not make ANALYZE stats steer the walk into a scan.

    With a full index, a user whose threads are all roots gets stats saying every
    lookup matches every thread, and each recursive step scans them all.
    """
    from api.measurement_scope import query_params as _query_params, scope_cte as _scope_cte

    params = _query_params({"user_id": "u", "scope_project": "all", "scope_agent": None,
                            "from": "2026-09-07", "to": "2026-09-21", "max_active_gap_seconds": 600})
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        db.executemany(
            "INSERT INTO threads (id,user_id,title,parent_thread_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            [(f"root-{n}", "u", "T", None, "t0", "t0") for n in range(200)],
        )
        db.execute("ANALYZE")

        scope = _plan(db, _scope_cte() + "SELECT id FROM scope_threads", params)
        recursive = scope[scope.index("RECURSIVE STEP") + 1:]
        assert any("SEARCH child USING INDEX idx_measurement_thread_children" in step for step in recursive), scope


def test_v77_to_v78_lands_only_the_frozen_index_set(tmp_path) -> None:
    path = tmp_path / "measurement-indexes-v77.db"
    _build_existing_v77_database(path)

    with closing(sqlite3.connect(path)) as db:
        migrate_77_to_78(db)
        assert _measurement_indexes(db) == _EXPECTED_INDEXES
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_llm_usage_priceable_measurement_start'"
        ).fetchone()[0] == 0


def test_v77_upgrade_through_v147_lands_the_same_indexes_a_fresh_install_has(tmp_path) -> None:
    path = tmp_path / "measurement-indexes-v77.db"
    _build_existing_v77_database(path)
    fresh = _fresh_all_indexes()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (
            True, 77, SCHEMA_VERSION,
        )
        assert _all_measurement_indexes(db) == fresh
        assert db.execute(
            "SELECT role, content FROM messages WHERE id = 'm-1'"
        ).fetchone() == ("user", "body")
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_the_step_skips_tables_a_database_lacks() -> None:
    """A partial database indexes what it has instead of failing the whole ladder."""
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE messages (id TEXT, thread_id TEXT, role TEXT, created_at TEXT)")
        migrate_77_to_78(db)

        assert _measurement_indexes(db) == {
            "idx_measurement_human_messages_time": _EXPECTED_INDEXES["idx_measurement_human_messages_time"],
        }


def test_the_step_repeats_cleanly_and_preserves_existing_indexes(tmp_path) -> None:
    """A half-applied v77 -> v78 must be safe to run again on the next startup."""
    path = tmp_path / "measurement-indexes-retry.db"
    _build_existing_v77_database(path)

    with closing(sqlite3.connect(path)) as db:
        migrate_77_to_78(db)
        once = _measurement_indexes(db)
        migrate_77_to_78(db)
        db.commit()

        assert once == _EXPECTED_INDEXES
        assert _measurement_indexes(db) == once
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        # The step itself never claims the version; only the ladder does.
        assert db.execute("PRAGMA user_version").fetchone()[0] == 77

        result = migrate_to_current(db, SCHEMA_VERSION)
        # The ladder still reports the upgrade after a retried step, and lands
        # exactly where a fresh install does: no extra index, no lost row.
        assert (result.migrated, result.from_version, result.to_version) == (
            True, 77, SCHEMA_VERSION,
        )
        assert _measurement_indexes(db) == _fresh_indexes()
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
