"""The schema 6 → 7 migration step: idle triggers, search index, ledger drop.

Three changes each needed a stopped instance, so they ride one step. The
runner owns the transaction, the backup, ``user_version`` and both validations
— its own tests cover those. These tests cover what the step itself must get
right, driven through the real runner rather than by calling the step directly.

The legacy DDL below is an explicit snapshot of schema 6 rather than an import
of current source, so these keep describing what we migrate *from* after the
source moves on.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

LEGACY_SCHEMA_6 = """
CREATE TABLE threads (id TEXT PRIMARY KEY, history_epoch INTEGER NOT NULL DEFAULT 0);
CREATE TABLE checkpoints (
    generation_order INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    history_epoch INTEGER NOT NULL,
    previous_generation_id TEXT,
    through_seq INTEGER NOT NULL,
    through_message_id TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual','pressure','model_switch')),
    mode TEXT NOT NULL CHECK (mode IN ('warm','cold')),
    status TEXT NOT NULL CHECK (
        status IN ('running','captured','installed','overtaken','failed','cancelled')
    ),
    source_harness TEXT NOT NULL,
    source_model TEXT NOT NULL,
    writer_harness TEXT NOT NULL,
    writer_model TEXT NOT NULL,
    originating_turn_id TEXT,
    first_included_seq INTEGER,
    continuity_base_revision INTEGER NOT NULL,
    continuity_base_snapshot_json TEXT NOT NULL,
    raw_result TEXT,
    continuity_snapshot_json TEXT,
    usage_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    captured_at TEXT,
    installed_at TEXT,
    ended_at TEXT
);
CREATE UNIQUE INDEX checkpoints_one_live
    ON checkpoints(thread_id) WHERE status IN ('running','captured');
CREATE INDEX checkpoints_current
    ON checkpoints(thread_id,history_epoch,status,generation_order DESC);
CREATE TABLE checkpoint_staged_edits (
    id TEXT PRIMARY KEY,
    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id) ON DELETE CASCADE,
    body TEXT
);
CREATE TABLE thread_context_declarations (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    declaration_class TEXT NOT NULL,
    statement TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _insert_checkpoint(db, cid: str, trigger: str = "pressure"):
    """Insert one checkpoint row in the shape capture and install leave it."""
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
        " status,source_harness,source_model,writer_harness,writer_model,"
        " continuity_base_revision,continuity_base_snapshot_json,raw_result,"
        " continuity_snapshot_json,started_at) "
        "VALUES (?,?,0,1,'m-1',?,'cold','installed','native','m','native','m',"
        " 0,'[]',?,?,'t0')",
        (cid, "t1", trigger, f"narrative for {cid}", '[{"statement":"kept"}]'),
    )


@pytest.fixture
def legacy_db(tmp_path) -> Path:
    """A schema-6 database carrying rows the migration must not damage."""
    path = tmp_path / "corsia.db"
    db = sqlite3.connect(path)
    try:
        db.executescript(LEGACY_SCHEMA_6)
        db.execute("INSERT INTO threads (id) VALUES ('t1')")
        for index in (1, 2, 3):
            _insert_checkpoint(db, f"cp-{index}")
        # A deletion leaves sqlite_sequence above max(generation_order); the
        # step must carry that forward or generation numbers get reused.
        db.execute("DELETE FROM checkpoints WHERE id='cp-3'")
        db.execute(
            "INSERT INTO checkpoint_staged_edits (id, checkpoint_id, body)"
            " VALUES ('s1','cp-1','staged')"
        )
        db.execute(
            "INSERT INTO thread_context_declarations"
            " (id,thread_id,declaration_class,statement,created_at)"
            " VALUES ('d1','t1','input_delivery_unconfirmed','stale','t0')"
        )
        db.execute("PRAGMA user_version=6")
        db.commit()
    finally:
        db.close()
    return path


def _migrate(path: Path):
    """Run the real ladder to 7, the way startup does."""
    from infra.database_migrations import migrate_to_current

    conn = sqlite3.connect(path)
    try:
        return migrate_to_current(conn, 7)
    finally:
        conn.close()


def _open(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def test_fresh_schema_accepts_an_idle_checkpoint():
    """The vocabulary widens at the source of truth, not only in the step."""
    from infra.checkpoint_schema import CHECKPOINT_SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        db.executescript(CHECKPOINT_SCHEMA_SQL)
        db.execute("INSERT INTO threads (id) VALUES ('t1')")
        _insert_checkpoint(db, "cp-idle", trigger="idle")
        assert db.execute(
            "SELECT trigger FROM checkpoints WHERE id='cp-idle'"
        ).fetchone()[0] == "idle"
    finally:
        db.close()


def test_fresh_schema_still_refuses_an_unknown_trigger():
    """Widening is exact: 'idle' joins the vocabulary, nothing else does."""
    from infra.checkpoint_schema import CHECKPOINT_SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        db.executescript(CHECKPOINT_SCHEMA_SQL)
        db.execute("INSERT INTO threads (id) VALUES ('t1')")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_checkpoint(db, "cp-bogus", trigger="whenever")
    finally:
        db.close()


def test_migration_widens_the_trigger_vocabulary(legacy_db):
    assert _migrate(legacy_db).migrated is True
    db = _open(legacy_db)
    try:
        _insert_checkpoint(db, "cp-idle", trigger="idle")
        assert db.execute(
            "SELECT trigger FROM checkpoints WHERE id='cp-idle'"
        ).fetchone()[0] == "idle"
    finally:
        db.close()


def test_migration_preserves_the_autoincrement_sequence(legacy_db):
    """A rebuild must not let generation_order be reused.

    The usage-discard bound is a generation number. If the rebuild reset the
    sequence after rows were deleted, a new checkpoint could take a number at
    or below an already-discarded bound and silently re-enter that band.
    """
    before = _open(legacy_db)
    try:
        seq_before = before.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='checkpoints'"
        ).fetchone()[0]
    finally:
        before.close()
    assert seq_before == 3  # three inserted, one deleted

    _migrate(legacy_db)

    db = _open(legacy_db)
    try:
        assert db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='checkpoints'"
        ).fetchone()[0] == seq_before
        _insert_checkpoint(db, "cp-next")
        assert db.execute(
            "SELECT generation_order FROM checkpoints WHERE id='cp-next'"
        ).fetchone()[0] == seq_before + 1
    finally:
        db.close()


def test_migration_preserves_every_checkpoint_row(legacy_db):
    """Rows survive the rebuild byte-identically, including their order."""
    query = (
        "SELECT generation_order, id, trigger, status, raw_result,"
        " continuity_snapshot_json FROM checkpoints ORDER BY generation_order"
    )
    before = _open(legacy_db)
    try:
        rows_before = [tuple(r) for r in before.execute(query)]
    finally:
        before.close()

    _migrate(legacy_db)

    db = _open(legacy_db)
    try:
        assert [tuple(r) for r in db.execute(query)] == rows_before
    finally:
        db.close()


def test_migration_keeps_the_staging_foreign_key_intact(legacy_db):
    """Rebuilding the referenced table must not orphan staged continuity edits."""
    _migrate(legacy_db)
    db = _open(legacy_db)
    try:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute(
            "SELECT checkpoint_id FROM checkpoint_staged_edits WHERE id='s1'"
        ).fetchone()[0] == "cp-1"
    finally:
        db.close()


def test_migration_backfills_the_search_index(legacy_db):
    """Existing narratives are searchable, not only rows written afterwards."""
    _migrate(legacy_db)
    db = _open(legacy_db)
    try:
        hits = db.execute(
            "SELECT rowid FROM checkpoints_fts WHERE checkpoints_fts MATCH 'narrative'"
        ).fetchall()
        assert len(hits) == 2
    finally:
        db.close()


def test_migration_drops_the_orphaned_declaration_ledger(legacy_db):
    _migrate(legacy_db)
    db = _open(legacy_db)
    try:
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='thread_context_declarations'"
        ).fetchone() is None
    finally:
        db.close()


def test_migrated_database_matches_a_fresh_one(legacy_db):
    """The invariant the version bump exists to protect.

    A migrated database and a fresh one must present the same checkpoint
    schema. If they drift, one accepts writes the other rejects — exactly the
    failure the startup version gate exists to make loud.

    Every object the step creates is compared, not just the two tables: the
    step's DDL is deliberately a frozen schema-7 copy. Later trigger changes
    have their own migrations and must not rewrite this historical contract.
    """
    from infra.database_migrations import _CHECKPOINTS_7_DDL, _CHECKPOINTS_7_INDEXES
    from infra.db_fts import _init_checkpoints_fts
    from infra.schema import SCHEMA_VERSION

    _migrate(legacy_db)

    fresh = sqlite3.connect(":memory:")
    migrated = _open(legacy_db)
    try:
        fresh.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        fresh.executescript(_CHECKPOINTS_7_DDL)
        fresh.execute("ALTER TABLE checkpoints_new RENAME TO checkpoints")
        for statement in _CHECKPOINTS_7_INDEXES:
            fresh.execute(statement)
        _init_checkpoints_fts(fresh)

        def shape(conn, name):
            """Normalized DDL: ignores cosmetics, catches real schema drift.

            `ALTER TABLE ... RENAME` quotes the table name and the fresh path
            carries `IF NOT EXISTS`; neither changes what the schema accepts.
            """
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (name,)
            ).fetchone()
            assert row is not None, f"{name} missing"
            sql = row["sql"] if isinstance(row, sqlite3.Row) else row[0]
            flat = "".join(sql.split()).replace("IFNOTEXISTS", "").replace('"', "")
            # Schema 43 appends two snapshot columns (#1602); this test freezes
            # the schema-7 table, so compare without them.
            return flat.replace(
                ",artifact_snapshot_jsonTEXT,plan_status_snapshot_jsonTEXT", ""
            )

        for name in (
            "checkpoints",
            "checkpoints_one_live",
            "checkpoints_current",
            "checkpoints_fts",
            "checkpoints_fts_insert",
            "checkpoints_fts_delete",
            "checkpoints_fts_update",
        ):
            assert shape(migrated, name) == shape(fresh, name), name
        # Steps 8, 9, and 11 through 15 do not touch checkpoint objects. Step
        # 10 adds one idle boundary index without changing the frozen schema-7
        # table or indexes above; each additive step has its own migration test.
        assert SCHEMA_VERSION == 129
    finally:
        fresh.close()
        migrated.close()
