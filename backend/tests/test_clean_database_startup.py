"""Startup contracts for the first-shipped schema and one recovery pass."""

from __future__ import annotations

import sqlite3

import pytest


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return db


def test_startup_creates_only_an_empty_database_or_opens_the_exact_version() -> None:
    """Runtime startup never upgrades or silently repairs an older database."""
    from infra.database import SchemaVersionError, initialize_schema
    from infra.schema import SCHEMA_VERSION

    fresh = _db()
    exact = _db()
    old = _db()
    try:
        initialize_schema(fresh)
        assert fresh.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert fresh.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone() is not None
        sequence_index = next(
            row for row in fresh.execute("PRAGMA index_list(messages)")
            if row["name"] == "idx_messages_session_seq"
        )
        assert sequence_index["unique"] == 1

        schema_objects = {
            row["name"]
            for row in fresh.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        assert "context_boundary_snapshots" not in schema_objects
        assert not any(
            name.startswith("idx_context_boundary_snapshots_")
            for name in schema_objects
        )
        assert "messages_fts" in schema_objects
        message_fts_triggers = {
            row["name"]
            for row in fresh.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'messages_fts_%'"
            )
        }
        assert message_fts_triggers == {
            "messages_fts_delete",
            "messages_fts_insert",
            "messages_fts_update",
        }
        thread_columns = {
            row["name"] for row in fresh.execute("PRAGMA table_info(threads)")
        }
        assert "runtime_context_epoch" in thread_columns
        assert "last_context_change_seq" in thread_columns
        assert "last_dynamic_tail_hash" not in thread_columns

        initialize_schema(exact)
        initialize_schema(exact)

        old.execute("CREATE TABLE retained_user_data (value TEXT)")
        old.execute("INSERT INTO retained_user_data VALUES ('keep me')")
        old.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        # A database written by a NEWER build is still refused, and now says so
        # accurately: migrations are forward-only, so there is nothing to run.
        with pytest.raises(SchemaVersionError, match="must not downgrade"):
            initialize_schema(old)
        assert old.execute(
            "SELECT value FROM retained_user_data"
        ).fetchone()[0] == "keep me"
        assert old.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone() is None
    finally:
        fresh.close()
        exact.close()
        old.close()


def test_fresh_database_fts_failure_rolls_back_the_whole_install(monkeypatch) -> None:
    """A failed first FTS install remains an empty database that can retry."""
    import infra.database as database

    db = _db()
    original_init_fts = database._init_fts

    def fail_fts_install(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE partial_fts_install (value TEXT)")
        raise sqlite3.OperationalError("injected FTS install failure")

    monkeypatch.setattr(database, "_init_fts", fail_fts_install)
    try:
        with pytest.raises(
            sqlite3.OperationalError,
            match="injected FTS install failure",
        ):
            database.initialize_schema(db)

        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE name IN ('messages', 'messages_fts', 'partial_fts_install')"
        ).fetchone()[0] == 0

        monkeypatch.setattr(database, "_init_fts", original_init_fts)
        assert database.initialize_schema(db) is True
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='messages_fts'"
        ).fetchone() is not None
    finally:
        db.close()


def test_existing_database_startup_does_not_scan_every_btree(monkeypatch) -> None:
    """Normal startup validates the schema version without a full DB scan."""
    import connections
    import infra.database as database
    import infra.subagent_caps as subagent_caps
    from infra.schema import SCHEMA_VERSION

    raw = _db()
    raw.execute("CREATE TABLE existing_data (value TEXT)")
    raw.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    class Connection:
        def execute(self, sql, *args, **kwargs):
            assert "quick_check" not in str(sql).lower()
            return raw.execute(sql, *args, **kwargs)

        def close(self):
            return None

        def __getattr__(self, name):
            return getattr(raw, name)

    monkeypatch.setattr(database, "get_db", lambda: Connection())
    monkeypatch.setattr(connections, "seed_builtin_connections", lambda: None)
    monkeypatch.setattr(subagent_caps, "seed_subagent_caps", lambda: None)
    try:
        database.init_db()
    finally:
        raw.close()


def test_schema_v2_database_refuses_because_no_migration_reaches_it() -> None:
    """Startup migrates forward, but only along the registered ladder.

    A version with no registered step is refused rather than guessed at, and
    the refusal leaves user data untouched.
    """
    from infra.database import SchemaVersionError, initialize_schema

    old = _db()
    try:
        old.execute("CREATE TABLE retained_user_data (value TEXT)")
        old.execute("INSERT INTO retained_user_data VALUES ('keep me')")
        old.execute("PRAGMA user_version=2")

        with pytest.raises(
            SchemaVersionError,
            match="no migration registered from schema version 2",
        ):
            initialize_schema(old)

        assert old.execute(
            "SELECT value FROM retained_user_data"
        ).fetchone()[0] == "keep me"
    finally:
        old.close()


def test_schema_v4_database_refuses_because_no_migration_reaches_it() -> None:
    """Same contract one version later: an unreachable version is not guessed."""
    from infra.database import SchemaVersionError, initialize_schema

    old = _db()
    try:
        old.execute("CREATE TABLE retained_user_data (value TEXT)")
        old.execute("INSERT INTO retained_user_data VALUES ('keep me')")
        old.execute("PRAGMA user_version=4")

        with pytest.raises(
            SchemaVersionError,
            match="no migration registered from schema version 4",
        ):
            initialize_schema(old)

        assert old.execute(
            "SELECT value FROM retained_user_data"
        ).fetchone()[0] == "keep me"
    finally:
        old.close()


def _seed_thread(db: sqlite3.Connection, thread_id: str) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?, 'user-1', 'claude-code/opus', 't0', 't0')",
        (thread_id,),
    )


def test_startup_retires_an_unrecognized_legacy_turn_and_still_recovers_others() -> None:
    """A leftover row from a status no current code writes must not wedge boot,
    and must not permanently block its own thread from starting another Turn."""
    from infra.database import initialize_schema
    from infra.input_admission import accept_thread_input
    from infra.input_writes import begin_batch_write
    from infra.migration_27_to_28_retire_waiting_retry import migrate_27_to_28
    from infra.thread_driver import TerminalResult, admit_input
    from infra.thread_recovery import recover_threads
    from infra.thread_inputs import start_idle_turn

    db = _db()
    try:
        initialize_schema(db)
        _seed_thread(db, "thread-legacy")
        _seed_thread(db, "thread-live")

        # A row in a status the current code no longer produces or interprets,
        # with a batch that fails the retired recovery branch's own precondition.
        db.commit()
        admit_input(
            "thread-legacy", role="user", content="stuck input", intent="apply_now",
            message_id="legacy-input", turn_id="legacy-turn", boot_epoch="ancient-boot",
            admitted_at="t0", db=db,
        )
        db.execute(
            "UPDATE active_turns SET status='waiting_retry',retry_at='2099' "
            "WHERE id='legacy-turn'",
        )

        # An ordinary orphaned Turn from a dead boot that must still be recovered.
        db.commit()
        accept_thread_input(
            "thread-live", role="user", content="live input", intent="apply_now",
            message_id="live-input", db=db,
        )
        batch = start_idle_turn(
            "thread-live", turn_id="live-turn", boot_epoch="dead-boot",
            started_at="t1", db=db,
        )
        assert batch is not None
        begin_batch_write(batch, attempt_id="dead-attempt", started_at="t2", db=db)
        db.commit()

        # The schema-migration phase of boot runs before thread recovery.
        migrate_27_to_28(db)
        db.commit()

        recovery = recover_threads(
            boot_epoch="new-boot", recovered_at="t3",
            successor_id=lambda _thread_id: "live-turn-2", db=db,
        )

        assert [item.result for item in recovery.settlements] == [
            TerminalResult.APPLIED, TerminalResult.APPLIED,
        ]
        # (a) The legacy row no longer carries the retired status -- the
        # migration folded it into an ordinary dead-boot orphan, and recovery
        # settled it exactly like thread-live's.
        assert db.execute(
            "SELECT status FROM active_turns WHERE id='legacy-turn'",
        ).fetchone()[0] == "error"
        assert db.execute(
            "SELECT status FROM active_turns WHERE id='live-turn'",
        ).fetchone()[0] == "error"
        # (b) Its stuck input fails visibly -- settle_unwritten_turn_inputs
        # marks a queued input under an errored Turn 'failed', it is not
        # silently dropped and not silently redelivered.
        assert db.execute(
            "SELECT delivery_state FROM messages WHERE id='legacy-input'",
        ).fetchone()[0] == "failed"
        # (c) The thread itself is free again: nothing still occupies its one
        # active-Turn slot, so it can start a fresh Turn after boot. Before the
        # migration existed, the legacy row still matched
        # idx_active_turns_one_active's ('running','waiting_retry') predicate
        # while start_idle_turn's guard only checked 'running', so this insert
        # raised sqlite3.IntegrityError and the thread could never start again.
        reopened = admit_input(
            "thread-legacy", role="user", content="new input", intent="apply_now",
            message_id="new-input", turn_id="legacy-turn-2", boot_epoch="new-boot",
            admitted_at="t4", db=db,
        )
        assert reopened.batch is not None
    finally:
        db.close()


def test_one_startup_pass_quarantines_unknown_writes_and_starts_the_fifo() -> None:
    """A dead process cannot replay its write or leave its successor ownerless."""
    from infra.database import initialize_schema
    from infra.input_writes import begin_batch_write
    from infra.thread_recovery import recover_threads
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = _db()
    try:
        initialize_schema(db)
        _seed_thread(db, "thread-1")
        accept_thread_input(
            "thread-1", role="user", content="possibly sent", intent="apply_now",
            message_id="input-1", db=db,
        )
        db.commit()
        batch = start_idle_turn(
            "thread-1", turn_id="turn-1", boot_epoch="dead-boot",
            started_at="t1", db=db,
        )
        assert batch is not None
        begin_batch_write(batch, attempt_id="write-1", started_at="t2", db=db)
        accept_thread_input(
            "thread-1", role="user", content="next", intent="queue_after_turn",
            message_id="input-2", db=db,
        )

        recovery = recover_threads(
            boot_epoch="new-boot",
            recovered_at="t3",
            successor_id=lambda _thread_id: "turn-2",
            db=db,
        )
        assert [
            item.successor.turn_id
            for item in recovery.settlements
            if item.successor is not None
        ] == ["turn-2"]
        assert [tuple(row) for row in db.execute(
            "SELECT id,delivery_state,write_outcome,active_turn_id "
            "FROM messages ORDER BY input_order"
        )] == [
            ("input-1", "ambiguous", "ambiguous", "turn-1"),
            ("input-2", "queued", None, "turn-2"),
        ]
        assert [tuple(row) for row in db.execute(
            "SELECT id,status,boot_epoch FROM active_turns ORDER BY turn_order"
        )] == [
            ("turn-1", "error", "dead-boot"),
            ("turn-2", "running", "new-boot"),
        ]

        repeated = recover_threads(
            boot_epoch="new-boot",
            recovered_at="t4",
            successor_id=lambda _thread_id: "turn-3",
            db=db,
        )
        assert repeated.settlements == ()
    finally:
        db.close()
