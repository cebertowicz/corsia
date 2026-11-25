"""Schema 198 -> 199 gives restart replay dead letters written before codes their reason code."""

import json
import sqlite3

from automation import restart_webhooks
from infra.database_migrations import MIGRATIONS
from infra.migration_198_to_199_restart_dead_letter_codes import (
    migrate_198_to_199,
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


def _row(db, row_id, event, external_id, reason, payload="{}"):
    db.execute(
        "INSERT INTO dead_letter_events (id,internal_event,external_id,reason,payload_json,status,created_at) "
        "VALUES (?,?,?,?,?,'parked','2026-11-10T00:00:00Z')",
        (row_id, event, external_id, reason, payload),
    )


def test_step_is_registered_at_198():
    assert SCHEMA_VERSION == 209
    assert MIGRATIONS[198] is migrate_198_to_199


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

    migrate_198_to_199(db)

    reasons = dict(db.execute("SELECT id, reason FROM dead_letter_events"))
    assert reasons == {old: CODE, done: CODE, "abc123def456": "no_owner", "other": "no_owner"}


def test_rows_written_before_199_keep_declared_names_private():
    db = _db()
    _row(db, "gh", "webhook.github.pr_opened", None, "no_owner")
    _row(db, "declared", "pr.merged", "id", "no_owner",
         json.dumps({"event_id": "id", "connector": "agent", "facts": {}}))

    migrate_198_to_199(db)
    migrate_198_to_199(db)

    assert dict(db.execute("SELECT id, code_named FROM dead_letter_events")) == {"gh": 1, "declared": 0}


def test_backfill_historical_writers_and_keep_ambiguous_envelopes_private():
    db = _db()
    def envelope(connector, event_id):
        return json.dumps({"connector": connector, "event_id": event_id, "facts": {}})

    rows = [
        ("platform", "email.received", "e1", envelope("gmail", "e1"), 1),
        ("declared", "email.received", "e2", envelope("my_agent", "e2"), 0),
        # A connector registered after declared intake may have been an agent namespace.
        ("later", "event", "e3", envelope("imap", "e3"), 0),
        ("router", "webhook.github.check_suite_completed", "e4", json.dumps({"repository": {}}), 1),
        ("watch", "automation.watch_expired", "e5", json.dumps({"watch_id": "w"}), 1),
        ("bulletin", "system_bulletin.infrastructure", "e6", json.dumps({"reason": "x", "item": "i"}), 1),
        ("failed_wake", "wake.failed", "e7", json.dumps({"message_id": "m", "summary": "x"}), 1),
        ("runner", "script.owner_gone", "e8", json.dumps({"schedule_id": "s", "summary": "x"}), 1),
        ("script_wake", "script.error", "e9", json.dumps({"owner": "o", "summary": "x"}), 1),
        ("quiet_watch", "github.ingress_overdue", None, json.dumps({"pull_requests": []}), 1),
        ("bad_facts", "secret", "e13", json.dumps({"event_id": "e13", "connector": "gmail", "facts": []}), 0),
        ("extra", "secret", "e14", json.dumps({"event_id": "e14", "connector": "gmail", "facts": {}, "x": 1}), 0),
        ("missing", "event", "e10", None, 0),
        ("malformed", "event", "e11", "{", 0),
        (restart_dead_letter_id("e12"), "webhook.github.push", "e12", envelope("gmail", "e12"), 0),
    ]
    for row_id, event, external, payload, _ in rows:
        reason = "check_suite_noise_parked" if row_id == "router" else "no_owner"
        _row(db, row_id, event, external, reason, payload)
    migrate_198_to_199(db)
    migrate_198_to_199(db)
    assert dict(db.execute("SELECT id, code_named FROM dead_letter_events")) == {
        row_id: expected for row_id, _, _, _, expected in rows
    }
    from infra.audit_set_aside import set_aside

    db.row_factory = sqlite3.Row
    reading = set_aside(db, "2026-11-09T00:00:00Z", "2026-11-11T00:00:00Z")
    assert any(row["internal_event"] == "webhook.github.check_suite_completed"
               and row["reason"] == "check_suite_noise_parked" for row in reading)


def test_an_install_without_the_table_is_left_alone():
    db = sqlite3.connect(":memory:")
    migrate_198_to_199(db)
    assert db.execute("SELECT count(*) FROM sqlite_master").fetchone() == (0,)
