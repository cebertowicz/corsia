"""Schema 126 -> 127: old correction notes move to the plural subjects contract, and two indexes land."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from core.lesson_corrections import current_owner_subjects
from core.correction_notes import noted_subjects
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_126_to_127_correction_note_subjects import migrate_126_to_127
from infra.schema import SCHEMA_VERSION

_INDEXES = ("idx_context_changes_note_key", "idx_plan_holds_user_ask_rounds")
_OLD_NOTE = {"chip": None, "kind": "correction_note", "correction_subject": "message:c1", "reoffer_unwritten": True}


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    return db


def _wound_back_to_v126() -> sqlite3.Connection:
    """A fresh database at 126: no new indexes, one old-shape note journaled and carried by a running turn's digest."""
    db = _fresh()
    for index in _INDEXES:
        db.execute(f"DROP INDEX {index}")
    db.execute("PRAGMA user_version=126")
    db.execute("INSERT INTO threads(id,user_id,created_at,updated_at) VALUES ('t1','u','t','t')")
    db.execute(
        "INSERT INTO context_change_journal(object_kind,object_id,change_kind,summary,details_json,created_at) "
        "VALUES ('thread_context','t1','note','old',?,'t')", (json.dumps(_OLD_NOTE),),
    )
    db.execute(
        "INSERT INTO active_turns(id,thread_id,execution_epoch,model,started_at,boot_epoch,status) "
        "VALUES ('turn','t1',1,'m','t','b','running')"
    )
    digest = {"event_type": "context.digest", "context_events": [{"details": dict(_OLD_NOTE)}]}
    db.execute(
        "INSERT INTO messages(id,thread_id,role,content,created_at,metadata,active_turn_id,input_order) "
        "VALUES ('context-digest:1','t1','system','digest','t',?,'turn',1)", (json.dumps(digest),),
    )
    db.commit()
    return db


def _journal_details(db: sqlite3.Connection) -> list[dict]:
    return [json.loads(row[0]) for row in db.execute("SELECT details_json FROM context_change_journal ORDER BY seq")]


def _index_sql(db: sqlite3.Connection) -> dict[str, str]:
    marks = ",".join("?" * len(_INDEXES))
    return dict(db.execute(f"SELECT name, sql FROM sqlite_master WHERE name IN ({marks})", _INDEXES).fetchall())


def test_the_step_is_registered_at_126() -> None:
    assert SCHEMA_VERSION == 127
    assert MIGRATIONS[126] is migrate_126_to_127


def test_an_old_note_and_its_digest_still_link_to_their_correction() -> None:
    with closing(_wound_back_to_v126()) as db:
        # Before the step, the plural-only reader no longer sees the old digest's correction.
        assert current_owner_subjects(db, "t1") == ()

        migrate_126_to_127(db)

        (details,) = _journal_details(db)
        assert "correction_subject" not in details
        assert (details["note_key"], details["correction_subjects"]) == ("message:c1", ["message:c1"])
        metadata = db.execute("SELECT metadata FROM messages WHERE id='context-digest:1'").fetchone()[0]
        assert noted_subjects(metadata) == ("message:c1",)
        assert current_owner_subjects(db, "t1") == ("message:c1",)


def test_the_step_adds_the_indexes_a_fresh_schema_has() -> None:
    with closing(_fresh()) as fresh, closing(_wound_back_to_v126()) as db:
        assert _index_sql(db) == {}
        migrate_126_to_127(db)
        assert _index_sql(db) == _index_sql(fresh) and len(_index_sql(fresh)) == len(_INDEXES)


def test_the_step_is_safe_to_run_again_and_leaves_new_notes_alone() -> None:
    with closing(_wound_back_to_v126()) as db:
        new = {"kind": "correction_note", "note_key": "review-round:h", "correction_subjects": ["doc-comment:c"]}
        db.execute(
            "INSERT INTO context_change_journal(object_kind,object_id,change_kind,summary,details_json,created_at) "
            "VALUES ('thread_context','t1','note','new',?,'t')", (json.dumps(new),),
        )
        migrate_126_to_127(db)
        once = _journal_details(db)
        migrate_126_to_127(db)

        assert _journal_details(db) == once and once[1] == new


def test_the_runner_takes_a_126_database_to_current_and_stays_there() -> None:
    with closing(_wound_back_to_v126()) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 126, SCHEMA_VERSION)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_the_step_leaves_a_database_without_the_tables_alone() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        migrate_126_to_127(db)
        assert db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
