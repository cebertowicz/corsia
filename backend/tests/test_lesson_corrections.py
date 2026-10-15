from __future__ import annotations

import json
import sqlite3

from core.lesson_corrections import (
    CHECK_RETIREMENT_CONDITION,
    correction_facts,
    incident_detail_flags,
    merge_subjects,
    placement_check_counts,
    record_placement_check,
)


def test_subject_anchors_preserve_batched_order_and_dedupe():
    assert merge_subjects(("message:one", "message:two"), ("message:two", "decision:three")) == (
        "message:one", "message:two", "decision:three",
    )


def test_late_correction_fact_joins_the_stored_message_subject():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE outcome_events (
        id TEXT, event_name TEXT, retracted_at TEXT, subject_key TEXT, ts TEXT, attributes TEXT
    )""")
    db.execute(
        "INSERT INTO outcome_events VALUES (?, ?, NULL, ?, ?, ?)",
        ("fact-1", "measurement.correction", "message:two", "2026-10-15T00:00:00+00:00",
         json.dumps({"label": "correction"})),
    )

    assert correction_facts(db, ("message:one", "message:two")) == {
        "message:two": {"id": "fact-1", "ts": "2026-10-15T00:00:00+00:00"},
    }


def test_placement_check_flags_incident_details_without_rejecting_text():
    flags = incident_detail_flags(
        'On 2026-10-14, Alex Example fixed CORSIA-1234: "this exact long phrase should never be copied verbatim".',
        "The incident message said this exact long phrase should never be copied verbatim.",
    )

    assert {"date", "id", "name", "quote"} <= set(flags)
    assert "overrides exceed half of fires" in CHECK_RETIREMENT_CONDITION


def test_placement_check_counts_one_observation_with_multiple_flags_once():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE lesson_placement_checks (
        thread_id TEXT, history_epoch INTEGER, entry_id INTEGER, revision INTEGER,
        check_name TEXT, flag TEXT, override_reason TEXT, created_at TEXT,
        UNIQUE(thread_id, history_epoch, entry_id, revision, check_name, flag)
    )""")

    record_placement_check(
        db, thread_id="thread", history_epoch=0, entry_id=1, revision=1,
        flags=("date", "id"), override_reason=None,
    )
    record_placement_check(
        db, thread_id="thread", history_epoch=0, entry_id=1, revision=1,
        flags=("date", "id"), override_reason="needed for the trial",
    )

    assert placement_check_counts(db, ("thread",))["fires"] == 1
    assert placement_check_counts(db, ("thread",))["overrides"] == 1
    # scope=all can name more threads, and lessons more subjects, than SQLite binds.
    many = tuple(f"thread-{index}" for index in range(40_000))
    assert placement_check_counts(db, (*many, "thread"))["fires"] == 1
    db.execute("CREATE TABLE outcome_events (id TEXT, event_name TEXT, retracted_at TEXT, "
               "subject_key TEXT, ts TEXT, attributes TEXT)")
    assert correction_facts(db, tuple(f"message:{index}" for index in range(40_000))) == {}
