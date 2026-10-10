"""Schema v79 converts the stored former product name, or refuses with the store unchanged."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from infra.database_migrations import MigrationError, migrate_to_current
from infra.migration_78_to_79_stored_names import scan_stored_names
from infra.schema import SCHEMA_VERSION
from stored_names_fixtures import (
    ARTIFACT_ID,
    DEAD_LETTER_SEED,
    FROM_VERSION,
    HISTORY,
    NEW,
    OLD,
    REGISTRATION_SEED,
    RESTART_SEED,
    RESTART_TURN,
    SCOPE_ID,
    THREAD_ID,
    build_store,
    dead_letter,
    derived,
    installed_metadata,
    message,
    raw_metadata,
    registration_event,
    restart_registration,
    usage_metadata,
)


def _metadata(db: sqlite3.Connection) -> dict[str, str | None]:
    return dict(db.execute("SELECT id, metadata FROM messages"))


def _ids(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[0] for row in db.execute(f"SELECT id FROM {table}")}


def test_every_owner_converts_and_history_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "stored-names.db"
    build_store(path)

    with closing(sqlite3.connect(path)) as db:
        planned = scan_stored_names(db).counts()
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (
            True, FROM_VERSION, SCHEMA_VERSION,
        )
        stored = _metadata(db)
        # The converted rows equal what a build that always used the new name writes.
        assert stored["m-installed"] == installed_metadata(NEW)
        assert stored["m-usage"] == usage_metadata(NEW)
        # Only the name tokens moved: spacing, escapes and constants are kept.
        assert stored["m-raw"] == raw_metadata(NEW)
        assert stored["m-bare"] is None
        assert {key: stored[key] for key in HISTORY} == HISTORY
        assert db.execute("SELECT DISTINCT content FROM messages").fetchall() == [(f"said {OLD}",)]

        assert _ids(db, "dead_letter_events") == {
            derived(NEW, DEAD_LETTER_SEED, "delivery-old"),
            derived(NEW, DEAD_LETTER_SEED, "delivery-new"),
            "abcdef123456",
        }
        assert _ids(db, "artifact_registration_events") == {
            derived(NEW, REGISTRATION_SEED, f"{ARTIFACT_ID}:rehomed:cause-1"),
            derived(NEW, REGISTRATION_SEED,
                    f"{ARTIFACT_ID}:origin_node_retired:cause-2:{SCOPE_ID}"),
            derived(NEW, REGISTRATION_SEED, f"{ARTIFACT_ID}:pinned:cause-3"),
            "0123456789ab",
        }
        assert _ids(db, "restart_wake_registrations") == {
            derived(NEW, RESTART_SEED, f"external:{THREAD_ID}:{RESTART_TURN}"),
            derived(OLD, RESTART_SEED, f"external:{THREAD_ID}:turn-woken"),
        }
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        # The read-only scan counted exactly what the migration then converted,
        # and finds nothing left: a rerun would write no row.
        assert planned == {
            "metadata_rows": 3, "section_values": 7, "source_ids": 4,
            "dead_letters": 1, "dead_letters_current": 1,
            "registration_events": 2, "registration_events_current": 1,
            "registration_events_unmatched": 0,
            "restart_registrations": 1, "restart_registrations_current": 0, "refusals": 0,
        }
        assert scan_stored_names(db).counts() == {
            "metadata_rows": 0, "section_values": 0, "source_ids": 0,
            "dead_letters": 0, "dead_letters_current": 2,
            "registration_events": 0, "registration_events_current": 3,
            "registration_events_unmatched": 0,
            "restart_registrations": 0, "restart_registrations_current": 1, "refusals": 0,
        }
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_the_scan_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "stored-names.db"
    build_store(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    with closing(sqlite3.connect(path)) as db:
        assert scan_stored_names(db).metadata_rows == 3
        assert not db.in_transaction

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_a_row_that_also_quotes_the_tokens_as_content_keeps_the_content(tmp_path: Path) -> None:
    """The owner converts; the same key and value quoted by a tool call do not."""
    quoted = {"source_id": f"{OLD}:v1:context/A.md", "note": {"source_id": f"native:system:{OLD}"}}
    stored = json.dumps({
        "arguments": quoted,
        "_corsia_installed_context_sources": [
            {"source_id": f"{OLD}:v1:context/A.md", "origin_section": OLD, "metadata": quoted},
        ],
    })
    path = tmp_path / "stored-names.db"
    build_store(path, plant=lambda db: message(db, "m-quoted", stored))

    with closing(sqlite3.connect(path)) as db:
        migrate_to_current(db, SCHEMA_VERSION)
        converted = _metadata(db)["m-quoted"]

    assert converted == json.dumps({
        "arguments": quoted,
        "_corsia_installed_context_sources": [
            {"source_id": f"{NEW}:v1:context/A.md", "origin_section": NEW, "metadata": quoted},
        ],
    })


def _installed(*entries) -> str:
    return json.dumps({"_corsia_installed_context_sources": list(entries)})


def _usage(inventory) -> str:
    return json.dumps({"context_usage": {"context_snapshot": {"source_inventory": inventory}}})


_UNKNOWN_ID = str(uuid.uuid4())
REFUSALS = {
    "section key under an unknown owner": lambda db: message(
        db, "bad", json.dumps({"other_owner": [{"origin_section": OLD}]})),
    "section key nested inside an owner entry": lambda db: message(
        db, "bad", _installed({"origin_section": "project", "metadata": {"layer": OLD}})),
    "non-string at a section key": lambda db: message(
        db, "bad", _installed({"origin_section": [OLD]})),
    "mixed former and new section keys": lambda db: message(
        db, "bad", _installed({"origin_section": OLD}, {"origin_section": NEW})),
    "invalid JSON carrying the name": lambda db: message(
        db, "bad", '{"origin_section": "' + OLD + '"'),
    "metadata that is not text": lambda db: message(
        db, "bad", ('{"origin_section": "' + OLD + '"}').encode()),
    "installed sources that are not a list": lambda db: message(
        db, "bad", json.dumps({"_corsia_installed_context_sources": {"label": OLD}})),
    "installed entry that is not an object": lambda db: message(
        db, "bad", _installed(OLD)),
    "source inventory that is not a list": lambda db: message(
        db, "bad", _usage({"label": OLD})),
    "source id in an unknown form": lambda db: message(
        db, "bad", _installed({"source_id": f"{OLD}:weird", "origin_section": "project"})),
    "section key in delivery options": lambda db: message(
        db, "bad", None, delivery_options=json.dumps({"layer": OLD})),
    "section key in a column with no owner": lambda db: db.execute(
        "INSERT INTO context_bootloader_events (id,scope,target_id,action,files_json,"
        "created_at) VALUES ('e','project','p','set',?,'t0')",
        (json.dumps([{"origin_section": OLD}]),)),
    "36-character dead letter id from neither seed": lambda db: dead_letter(
        db, _UNKNOWN_ID, "delivery-x"),
    "dead letter stored under both seeds": lambda db: dead_letter(
        db, derived(NEW, DEAD_LETTER_SEED, "delivery-old"), "delivery-old"),
    "pending restart registration from neither seed": lambda db: restart_registration(
        db, _UNKNOWN_ID, "turn-x", "pending"),
    "pending restart registration stored under both seeds": lambda db: restart_registration(
        db, derived(NEW, RESTART_SEED, f"external:{THREAD_ID}:{RESTART_TURN}"),
        RESTART_TURN, "pending"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_an_unclassified_store_refuses_and_is_left_byte_identical(tmp_path: Path, case: str) -> None:
    path = tmp_path / "refused.db"
    build_store(path, plant=REFUSALS[case])
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    with closing(sqlite3.connect(path)) as db:
        assert scan_stored_names(db).refusals == 1
        with pytest.raises(MigrationError, match="rolled back, database unchanged"):
            migrate_to_current(db, SCHEMA_VERSION)
        assert db.execute("PRAGMA user_version").fetchone()[0] == FROM_VERSION

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_a_causal_event_whose_registration_was_deleted_is_kept_and_counted(
    tmp_path: Path,
) -> None:
    """Registrations are deleted, events are not: no caller can derive this id again."""
    orphan = derived(
        OLD, REGISTRATION_SEED, f"{ARTIFACT_ID}:origin_node_retired:cause-9:reg-gone",
    )
    path = tmp_path / "stored-names.db"
    build_store(path, plant=lambda db: registration_event(
        db, orphan, "origin_node_retired", "cause-9"))

    with closing(sqlite3.connect(path)) as db:
        planned = scan_stored_names(db)
        assert (planned.registration_events_unmatched, planned.refusals) == (1, 0)
        assert migrate_to_current(db, SCHEMA_VERSION).migrated
        assert orphan in _ids(db, "artifact_registration_events")
        assert scan_stored_names(db).registration_events_unmatched == 1


def test_a_refusal_names_the_row_and_never_its_content(tmp_path: Path) -> None:
    path = tmp_path / "refused.db"
    build_store(path, plant=lambda db: message(
        db, "bad", json.dumps({"secret": "hunter2", "other": {"layer": OLD}})))

    with closing(sqlite3.connect(path)) as db:
        with pytest.raises(MigrationError) as raised:
            migrate_to_current(db, SCHEMA_VERSION)

    assert "messages[bad]" in str(raised.value)
    assert "hunter2" not in str(raised.value)


def test_a_historical_store_without_these_tables_migrates(tmp_path: Path) -> None:
    """Older ladder fixtures build partial schemas; absence is not a refusal."""
    from infra.migration_78_to_79_stored_names import migrate_78_to_79

    with closing(sqlite3.connect(tmp_path / "partial.db")) as db:
        db.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, content TEXT)")
        db.execute("INSERT INTO messages VALUES ('m', ?)", (OLD,))
        migrate_78_to_79(db)
        assert db.execute("SELECT content FROM messages").fetchall() == [(OLD,)]
