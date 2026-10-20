from __future__ import annotations

import json
import sqlite3

import pytest

from core.correction_learning import record_terminal_outcome, record_triage


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE active_turns (id TEXT, thread_id TEXT, status TEXT, started_at TEXT, context_manifest_id TEXT);
        CREATE TABLE messages (thread_id TEXT, role TEXT, active_turn_id TEXT, metadata TEXT);
        CREATE TABLE context_manifests (id TEXT, entries_json TEXT);
        CREATE TABLE guidance_loads (id TEXT, turn_id TEXT, path TEXT, created_at TEXT);
        CREATE TABLE outcome_events (
            id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE, event_name TEXT, source TEXT, provenance TEXT,
            ts TEXT, observed_ts TEXT, subject_key TEXT, project_id TEXT, agent_thread_id TEXT,
            attribution TEXT, corroborated_by TEXT, attributes TEXT, retracted_at TEXT
        );
        CREATE TABLE outcome_event_counters (counter TEXT, event_name TEXT, agent_thread_id TEXT, day TEXT, n INTEGER,
            PRIMARY KEY (counter,event_name,agent_thread_id,day));
    """)
    db.execute("INSERT INTO active_turns VALUES ('turn','thread','running','2026-10-20T00:00:00+00:00','manifest')")
    db.execute("INSERT INTO context_manifests VALUES ('manifest',?)", (json.dumps([{"stable_reference": "skills/example/SKILL.md"}]),))
    db.execute("INSERT INTO outcome_events VALUES ('correction','fact','measurement.correction','agent','agent_recorded','2026-10-20T00:00:00+00:00','2026-10-20T00:00:00+00:00','message:1',NULL,'thread',NULL,NULL,?,NULL)", (json.dumps({"label":"correction"}),))
    return db


def test_triage_is_immutable_and_refuses_unloaded_target():
    db = _db()
    result = record_triage(db, thread_id="thread", subject_key="message:1", tier="miss", cause="process_gap", new_work="no", new_work_signal=None, target_file="skills/example/SKILL.md")
    assert result["target_file"] == "skills/example/SKILL.md"
    with pytest.raises(ValueError, match="already exists"):
        record_triage(db, thread_id="thread", subject_key="message:1", tier="miss", cause="process_gap", new_work="no", new_work_signal=None, target_file="skills/example/SKILL.md")
    with pytest.raises(ValueError, match="Choose one of: skills/example/SKILL.md"):
        record_triage(db, thread_id="thread", subject_key="message:1", tier="note", cause="process_gap", new_work="no", new_work_signal=None, target_file="skills/other/SKILL.md")


def test_terminal_outcome_is_immutable_and_dropped_needs_reason():
    db = _db()
    result = record_terminal_outcome(db, thread_id="thread", subject_key="message:1", outcome="fixed")
    assert result["outcome"] == "fixed"
    with pytest.raises(ValueError, match="already exists"):
        record_terminal_outcome(db, thread_id="thread", subject_key="message:1", outcome="watch")
    with pytest.raises(ValueError, match="requires reason"):
        record_terminal_outcome(db, thread_id="thread", subject_key="message:1", outcome="dropped")
