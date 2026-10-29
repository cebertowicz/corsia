from __future__ import annotations

import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION


def _row(db, row_id: str, source: str, model: str, cost: float | None) -> None:
    db.execute(
        """INSERT INTO llm_usage(id,thread_id,model,prompt_tokens,completion_tokens,
                                  total_tokens,cost_usd,source,created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (row_id, "thread", model, 11, 7, 18, cost, source, "2026-10-07T00:00:00+00:00"),
    )


def test_migration_clears_canonical_subscription_estimates_idempotently() -> None:
    with sqlite3.connect(":memory:") as db:
        initialize_schema(db)
        _row(db, "claude", "host_access_application_review", "claude-code/sonnet", 0.11)
        _row(db, "codex", "host_access_action_review", "codex/gpt-5.6-terra", 0.22)
        _row(db, "metered", "host_access_action_review", "openai/gpt-5", 0.33)
        _row(db, "other-source", "conversation", "claude-code/sonnet", 0.44)
        db.execute("PRAGMA user_version=91")
        db.commit()

        first_migration = migrate_to_current(db, SCHEMA_VERSION)
        first = db.execute(
            "SELECT id,prompt_tokens,completion_tokens,total_tokens,cost_usd "
            "FROM llm_usage ORDER BY id"
        ).fetchall()
        second_migration = migrate_to_current(db, SCHEMA_VERSION)
        second = db.execute(
            "SELECT id,prompt_tokens,completion_tokens,total_tokens,cost_usd "
            "FROM llm_usage ORDER BY id"
        ).fetchall()

    assert (first_migration.migrated, first_migration.from_version, first_migration.to_version) == (
        True, 91, SCHEMA_VERSION,
    )
    assert not second_migration.migrated
    assert first == second == [
        ("claude", 11, 7, 18, None),
        ("codex", 11, 7, 18, None),
        ("metered", 11, 7, 18, 0.33),
        ("other-source", 11, 7, 18, None),
    ]
