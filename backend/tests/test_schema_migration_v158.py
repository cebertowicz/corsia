"""Schema 157 -> 158: provider-failure rows written before cause kinds get one stored."""
from __future__ import annotations

import json
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_157_to_158_error_cause_kinds import migrate_157_to_158
from infra.schema import SCHEMA_VERSION

_EXPIRED = (
    "⚠️ Claude Code stopped: API Error: 401 Failed to authenticate. "
    "OAuth session expired and could not be refreshed."
)

# id -> (content, metadata, expected cause_kind; None means the row is untouched)
_ROWS: dict[str, tuple[str, object, str | None]] = {
    "stored-cause": ("⚠️ Claude Code stopped: x", {"type": "cc_error", "cc": True, "cause": "sign_in_expired"},
                     "sign_in_expired"),
    "expired-exit": (_EXPIRED, {"type": "cc_error", "harness": "claude-code", "cause": "process_exited"},
                     "sign_in_expired"),
    "plain-exit": ("⚠️ Codex stopped: exited", {"type": "cc_error", "codex": True, "cause": "process_exited"},
                   "process_exited"),
    "codex-limit": ("x", {"type": "cc_error", "harness": "codex", "cause": "usageLimitExceeded"}, "usage_limit"),
    "native": ("boom", {"type": "llm_error"}, "unknown"),
    "kept": ("x", {"type": "cc_error", "cause": "process_exited", "cause_kind": "rate_limited"}, "rate_limited"),
    "failed-result": ("x", {"type": "result", "cc": True, "is_error": True, "cause": "sign_in_expired"},
                      "sign_in_expired"),
    "expired-result": (_EXPIRED, {"type": "result", "cc": True, "is_error": True}, "sign_in_expired"),
    "stopped-result": ("x", {"type": "result", "cc": True, "is_error": True, "stop_reason": "interrupted"}, None),
    "done-result": ("x", {"type": "result", "cc": True, "is_error": False}, None),
    "codex-result": (_EXPIRED, {"type": "result", "codex": True, "is_error": True}, None),
    "boundary": ("x", {"type": "tool_boundary_violation"}, None),
    "not-object": ("x", [1], None),
}


def _legacy_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('t','u','T','x','x')")
    for row_id, (content, metadata, _) in _ROWS.items():
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES (?,?,'system',?,?,'x')",
            (row_id, "t", content, json.dumps(metadata)),
        )
    db.execute("PRAGMA user_version=157")
    db.commit()
    return db


def test_v158_is_registered() -> None:
    assert SCHEMA_VERSION == 160
    assert MIGRATIONS[157] is migrate_157_to_158


def test_v158_stamps_each_older_provider_failure_row_once_through_the_writers_mapping() -> None:
    db = _legacy_db()
    try:
        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 157, 160)
        stored = {row_id: json.loads(metadata) for row_id, metadata in db.execute("SELECT id, metadata FROM messages")}
        for row_id, (_, metadata, kind) in _ROWS.items():
            if kind is None:
                assert stored[row_id] == metadata, row_id
            else:
                assert isinstance(metadata, dict)
                assert stored[row_id] == {**metadata, "cause_kind": kind}, row_id
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
    finally:
        db.close()
