"""Per-call offers retain mid-Turn versions and honest missing lineage."""
from __future__ import annotations

import json
import sqlite3

import pytest

from core.lesson_report_receipts import read_offer_evidence, read_offers
from infra.schema import SCHEMA_SQL


def offer_counts(offers):
    """Distinct recorded calls, tasks and attempts of a list of offers: what the indexed counts must equal."""
    return {
        "calls": len({offer["usage_id"] for offer in offers}),
        "tasks": len({offer["task_id"] for offer in offers if offer.get("task_id")}),
        "attempts": len({offer["attempt_id"] for offer in offers if offer.get("attempt_id")}),
        "calls_without_task": len({offer["usage_id"] for offer in offers if not offer.get("task_id")}),
        "opportunity": None, "application": None, "efficacy": None,
        "reason": "indexed offers do not prove reading, use, or benefit",
    }


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


def manifest(db, identity, lessons):
    entries = [{"source_id": "dynamic:skills-index"}]
    if lessons is not None:
        entries[0]["lessons"] = lessons
    db.execute(
        "INSERT INTO context_manifests(id,harness,entries_json,entry_count,created_at) VALUES(?,'native',?,1,'t')",
        (identity, json.dumps(entries)),
    )


def call(db, identity, manifest_id, *, thread="owner", task="task", attempt="attempt"):
    attribution = ("plan", task, attempt, "commitment", "execution") if task else (None,) * 5
    db.execute(
        "INSERT INTO llm_usage(id,thread_id,turn_id,model,context_manifest_id,created_at,"
        "plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) "
        "VALUES(?,?,'turn','test/model',?,'2026-09-21',?,?,?,?,?)",
        (identity, thread, manifest_id, *attribution),
    )


def final_manifest(db, manifest_id):
    db.execute(
        "INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,context_manifest_id) "
        "VALUES('turn','owner',0,'test/model','running','2026-09-21','test-boot',?)",
        (manifest_id,),
    )


def read(db, threads=("owner",)):
    return read_offers(db, user_id="user", thread_ids=threads, from_ts="2026-09-21", to_ts="2026-09-28")


def test_lifetime_offer_stream_never_materializes_the_cursor(db):
    manifest(db, "manifest", [{"id": "lesson", "behavioral_sha256": "a" * 64}])
    call(db, "call", "manifest")

    class StreamingCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __iter__(self):
            return iter(self.cursor)

        def fetchall(self):
            pytest.fail("lifetime offer stream called fetchall")

    class StreamingDB:
        def execute(self, *args):
            return StreamingCursor(db.execute(*args))

    result = read_offer_evidence(
        StreamingDB(), user_id="user", thread_ids=("owner",),
        from_ts="2026-09-21", to_ts="2026-09-28",
    )
    assert result["retained"][0]["usage_id"] == "call"


def test_mid_turn_checkpoint_keeps_both_per_call_versions_and_distinct_attempts(db):
    manifest(db, "old", [{"id": "lesson", "behavioral_sha256": "a" * 64}])
    manifest(db, "new", [{"id": "lesson", "behavioral_sha256": "b" * 64}])
    call(db, "one", "old")
    call(db, "two", "old")
    call(db, "three", "new", attempt="retry")
    final_manifest(db, "new")
    result = read(db)
    assert [(row["usage_id"], row["version"]) for row in result["offers"]] == [
        ("one", "a" * 64), ("three", "b" * 64), ("two", "a" * 64),
    ]
    counts = offer_counts(result["offers"])
    assert counts["calls"] == 3 and counts["tasks"] == 1 and counts["attempts"] == 2
    assert counts["efficacy"] is None and counts["application"] is None
    assert all(row["raw_version"] is None and row["descriptor_tokens"] is None for row in result["offers"])


def test_final_turn_manifest_does_not_fill_an_unbound_call_or_old_metadata(db):
    manifest(db, "final", [{"id": "lesson", "behavioral_sha256": "a" * 64}])
    manifest(db, "legacy", None)
    manifest(db, "empty", [])
    call(db, "unbound", None)
    call(db, "legacy", "legacy")
    call(db, "empty", "empty")
    final_manifest(db, "final")
    result = read(db)
    assert result["offers"] == [] and result["calls"] == 3
    assert result["unavailable_calls"] == {"lesson metadata unavailable": 1, "per-call manifest unavailable": 1}


def test_caller_asserted_foreign_thread_and_orphan_id_do_not_disclose_a_manifest(db):
    manifest(db, "private", [{"id": "private-lesson", "behavioral_sha256": "a" * 64}])
    call(db, "foreign", "private", thread="foreign")
    result = read(db, ("foreign", "bare-orphan"))
    assert result == {"offers": [], "calls": 0, "unavailable_calls": {}}


def test_duplicate_manifest_membership_is_one_offer_and_missing_task_is_named(db):
    lesson = {"id": "lesson", "behavioral_sha256": "a" * 64}
    manifest(db, "manifest", [lesson, lesson])
    call(db, "call", "manifest", task=None)
    result = read(db)
    assert len(result["offers"]) == 1
    assert offer_counts(result["offers"])["calls_without_task"] == 1
