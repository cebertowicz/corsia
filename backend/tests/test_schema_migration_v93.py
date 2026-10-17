"""Schema 93 clears only canonical historical subscription estimates."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database_migrations import MIGRATIONS
from infra.migration_92_to_93_host_access_subscription_costs import migrate_92_to_93
from infra.schema import SCHEMA_VERSION


def test_v93_clears_canonical_subscription_costs_preserving_tokens_idempotently():
    assert SCHEMA_VERSION == 124
    assert MIGRATIONS[92] is migrate_92_to_93
    rows = [
        ("host_access_application_review", "claude-code/sonnet", 0.11, 11, 1),
        ("host_access_action_review", "codex/gpt-6.1-sol", 0.22, 12, 2),
        ("conversation", "claude-code/sonnet", 0.33, 13, 3),
        ("conversation", "codex/gpt-6.1-sol", 0.44, 14, 4),
        ("external_harness:claude-code", "claude-code/sonnet", 0.55, 15, 5),
        ("external_harness:codex", "codex/gpt-6.1-sol", 0.66, 16, 6),
        ("checkpoint", "claude-code/sonnet", 0.77, 17, 7),
        ("checkpoint", "codex/gpt-6.1-sol", 0.88, 18, 8),
        # Native transport aliases, local, foreign, and unknown model identities
        # carry no historical canonical harness evidence and remain unchanged.
        ("conversation", "anthropic/claude-sonnet-4-6", 0.99, 19, 9),
        ("conversation", "openai/gpt-6.1", 1.10, 20, 10),
        ("conversation", "omlx@local/model", 1.21, 21, 11),
        ("conversation", "external@ns-foo/model", 1.32, 22, 12),
        ("conversation", "unknown", 1.43, 23, 13),
    ]
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute(
            "CREATE TABLE llm_usage "
            "(source TEXT, model TEXT, cost_usd REAL, prompt_tokens INTEGER, completion_tokens INTEGER)"
        )
        db.executemany("INSERT INTO llm_usage VALUES (?,?,?,?,?)", rows)

        migrate_92_to_93(db)
        migrate_92_to_93(db)

        assert db.execute(
            "SELECT source, model, cost_usd, prompt_tokens, completion_tokens "
            "FROM llm_usage ORDER BY rowid"
        ).fetchall() == [
            (source, model, None if index < 8 else cost, prompt, completion)
            for index, (source, model, cost, prompt, completion) in enumerate(rows)
        ]
