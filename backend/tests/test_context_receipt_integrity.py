from __future__ import annotations

import json
import sqlite3

import pytest


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            seq INTEGER
        );
        CREATE TABLE active_turns (
            turn_order INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        """
    )
    return connection


def _identity():
    from infra.context_receipts import ContextReceiptIdentity

    return ContextReceiptIdentity("codex", "codex/luna", 3, "a" * 64)


def _usage() -> dict:
    return {
        "model_context_window": 262_144,
        "total_used": 12_000,
        "token_usage_available": True,
        "token_source": "measured",
    }


@pytest.mark.parametrize(
    "metadata",
    ["{broken", "[]", json.dumps({"reasoning": "missing turn"})],
)
def test_receipt_writer_preserves_ineligible_metadata(metadata: str):
    from infra.context_receipts import attach_context_receipt

    connection = _connection()
    connection.execute(
        "INSERT INTO messages VALUES "
        "('assistant-1','thread-1','assistant','answer',?,'now',1)",
        (metadata,),
    )

    receipt = attach_context_receipt(
        "thread-1",
        "assistant-1",
        context_usage=_usage(),
        identity=_identity(),
        db=connection,
    )

    stored = connection.execute(
        "SELECT metadata FROM messages WHERE id='assistant-1'"
    ).fetchone()["metadata"]
    assert receipt is None
    assert stored == metadata


class _CountingConnection:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.selects = 0

    def execute(self, sql: str, parameters=()):
        if sql.lstrip().upper().startswith("SELECT"):
            self.selects += 1
        return self.connection.execute(sql, parameters)


def test_latest_receipt_uses_one_database_snapshot():
    from infra.context_receipts import (
        attach_context_receipt,
        latest_measured_context_receipt,
    )

    connection = _connection()
    connection.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,status,started_at,ended_at) "
        "VALUES ('turn-1','thread-1','completed','start','end')"
    )
    connection.execute(
        "INSERT INTO messages VALUES "
        "('assistant-1','thread-1','assistant','answer',?,'now',1)",
        (json.dumps({"turn_id": "turn-1"}),),
    )
    attach_context_receipt(
        "thread-1",
        "assistant-1",
        context_usage=_usage(),
        identity=_identity(),
        db=connection,
    )
    counted = _CountingConnection(connection)

    selected = latest_measured_context_receipt(
        "thread-1", _identity(), db=counted
    )

    assert selected is not None
    assert selected["message_id"] == "assistant-1"
    assert counted.selects == 1
