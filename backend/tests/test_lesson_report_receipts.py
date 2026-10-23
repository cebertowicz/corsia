"""Task-start receipts are the sole source of indexed lesson offers."""
from __future__ import annotations

import sqlite3

import pytest

from core.lesson_report_receipts import read_offer_evidence, read_offers
from infra.schema import SCHEMA_SQL
from tests.lesson_receipt_helpers import write_trial_receipt


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_SQL)
    for thread, user in (("owner", "user"), ("foreign", "another")):
        connection.execute(
            "INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES(?,?,'test/model','t','t')",
            (thread, user),
        )
    yield connection
    connection.close()


def call(db, identity, *, thread="owner", manifest_id=None):
    """Record a real scoped LLM call; offer membership must not inspect its manifest."""
    db.execute(
        "INSERT INTO llm_usage(id,thread_id,turn_id,model,context_manifest_id,created_at) "
        "VALUES(?,?,'turn','test/model',?,'2026-09-21')",
        (identity, thread, manifest_id),
    )


def read(db, threads=("owner",)):
    return read_offers(db, user_id="user", thread_ids=threads, from_ts="2026-09-21", to_ts="2026-09-28")


def test_lifetime_receipt_stream_never_materializes_the_cursor(db):
    receipt_id = write_trial_receipt(db, created_at="2026-09-21")

    class StreamingCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __iter__(self):
            return iter(self.cursor)

        def fetchone(self):
            return self.cursor.fetchone()

        def fetchall(self):
            pytest.fail("lifetime receipt stream called fetchall")

    class StreamingDB:
        def execute(self, *args):
            return StreamingCursor(db.execute(*args))

    result = read_offer_evidence(
        StreamingDB(), user_id="user", thread_ids=("owner",),
        from_ts="2026-09-21", to_ts="2026-09-28",
    )
    assert result["retained"][0]["receipt_id"] == receipt_id


def test_task_start_receipts_keep_versions_and_receipt_native_counts(db):
    first = write_trial_receipt(
        db, behavioral_sha256="a" * 64, node_id="task-a", checkpoint_generation_id="install-a",
        created_at="2026-09-21T00:00:00",
    )
    second = write_trial_receipt(
        db, behavioral_sha256="a" * 64, node_id="task-a", checkpoint_generation_id="install-b",
        created_at="2026-09-21T00:00:01",
    )
    third = write_trial_receipt(
        db, behavioral_sha256="b" * 64, node_id="task-b", checkpoint_generation_id="install-c",
        created_at="2026-09-21T00:00:02",
    )

    result = read(db)

    assert [(row["receipt_id"], row["version"]) for row in result["offers"]] == [
        (first, "a" * 64), (second, "a" * 64), (third, "b" * 64),
    ]
    assert all(row["thread_id"] == "owner" and row["turn_id"] is None for row in result["offers"])
    assert [row["task_id"] for row in result["offers"]] == ["task-a", "task-a", "task-b"]
    evidence = read_offer_evidence(
        db, user_id="user", thread_ids=("owner",), from_ts="2026-09-21", to_ts="2026-09-28",
    )
    assert evidence["period"]["offers"].counts == {
        ("lesson", "a" * 64): {"n": 2, "receipts": 2, "tasks": 1},
        ("lesson", "b" * 64): {"n": 1, "receipts": 1, "tasks": 1},
    }


def test_unbound_legacy_and_empty_calls_are_known_zero_without_with_receipts(db):
    call(db, "unbound")
    call(db, "legacy", manifest_id="legacy")
    call(db, "empty", manifest_id="empty")

    result = read(db)

    assert result["offers"] == []
    assert result["calls"] == 3
    assert result["unavailable_calls"] == {}


def test_caller_asserted_foreign_thread_and_orphan_id_do_not_disclose_receipts(db):
    write_trial_receipt(db, lesson_id="private-lesson", thread_id="foreign", created_at="2026-09-21")
    write_trial_receipt(db, lesson_id="orphan-lesson", thread_id="bare-orphan", created_at="2026-09-21")

    result = read(db, ("foreign", "bare-orphan"))

    assert result == {"offers": [], "calls": 0, "unavailable_calls": {}}


def test_only_with_trial_receipts_are_offers_and_keep_the_task_reference(db):
    offered = write_trial_receipt(db, node_id="offered-task", variant="with", created_at="2026-09-21")
    write_trial_receipt(db, node_id="without-task", variant="without", created_at="2026-09-21")
    write_trial_receipt(db, node_id="crossover-task", variant="crossover", created_at="2026-09-21")
    write_trial_receipt(db, node_id="omitted-task", variant=None, created_at="2026-09-21")

    result = read(db)

    assert [(row["receipt_id"], row["task_id"]) for row in result["offers"]] == [(offered, "offered-task")]

    # Incomplete historical provenance stays absent; a receipt never invents an attempt.
    db.execute("UPDATE guidance_loads SET node_id=NULL WHERE id=?", (offered,))
    [unbound] = read(db)["offers"]
    assert unbound["task_id"] is None and "attempt_id" not in unbound and "model" not in unbound
    evidence = read_offer_evidence(
        db, user_id="user", thread_ids=("owner",), from_ts="2026-09-21", to_ts="2026-09-28",
    )
    assert evidence["period"]["offers"].counts[("lesson", "a" * 64)] == {"n": 1, "receipts": 1, "tasks": 0}
