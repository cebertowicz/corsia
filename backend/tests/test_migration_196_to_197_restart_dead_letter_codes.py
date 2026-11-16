"""Schema 196 -> 197 gives restart replay dead letters written before codes their reason code."""

import sqlite3

from automation import restart_webhooks
from infra.database_migrations import MIGRATIONS
from infra.migration_196_to_197_restart_dead_letter_codes import (
    migrate_196_to_197,
    restart_dead_letter_id,
)
from infra.schema import SCHEMA_VERSION

CODE = restart_webhooks.DEAD_LETTER_REASON


def _db():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE dead_letter_events (id TEXT PRIMARY KEY, internal_event TEXT NOT NULL, "
        "external_id TEXT, reason TEXT NOT NULL, payload_json TEXT, "
        "status TEXT NOT NULL DEFAULT 'parked', created_at TEXT NOT NULL)"
    )
    return db


def _row(db, row_id, event, external_id, reason):
    db.execute(
        "INSERT INTO dead_letter_events (id,internal_event,external_id,reason,payload_json,status,created_at) "
        "VALUES (?,?,?,?,'{}','parked','2026-11-10T00:00:00Z')",
        (row_id, event, external_id, reason),
    )


def test_step_is_registered_at_196():
    assert SCHEMA_VERSION == 197
    assert MIGRATIONS[196] is migrate_196_to_197


def test_the_frozen_id_is_the_one_restart_webhooks_writes():
    expected = restart_webhooks.uuid.uuid5(
        restart_webhooks.uuid.NAMESPACE_URL,
        f"{restart_webhooks._DEAD_LETTER_SEED}:delivery-1",
    )
    assert restart_dead_letter_id("delivery-1") == str(expected)


def test_only_restart_rows_holding_text_are_rewritten():
    db = _db()
    old = restart_dead_letter_id("d-old")
    done = restart_dead_letter_id("d-done")
    _row(db, old, "webhook.github.push", "d-old", "stored webhook payload is not an object")
    _row(db, done, "webhook.github.push", "d-done", CODE)
    # Same event kind and an external id, but not a restart dead letter's id.
    _row(db, "abc123def456", "webhook.github.pr_opened", "d-old", "no_owner")
    _row(db, "other", "email.received", None, "no_owner")

    migrate_196_to_197(db)

    reasons = dict(db.execute("SELECT id, reason FROM dead_letter_events"))
    assert reasons == {old: CODE, done: CODE, "abc123def456": "no_owner", "other": "no_owner"}


def test_an_install_without_the_table_is_left_alone():
    db = sqlite3.connect(":memory:")
    migrate_196_to_197(db)
    assert db.execute("SELECT count(*) FROM sqlite_master").fetchone() == (0,)
