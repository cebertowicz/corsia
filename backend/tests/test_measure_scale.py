"""measure_status reads one agent's recent rows and counters by index, never by scanning the tables."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from infra.measure_registry import _OWNER_ROWS, NAMESPACE_ROWS
from infra.measure_status import COUNTERS_SQL, ROWS_SQL, status, synced_files
from infra.schema import SCHEMA_SQL

AGENT, OTHER = "agent-a", "agent-b"


def world() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA_SQL)
    return db


def plan(db: sqlite3.Connection, sql: str, args: tuple) -> list[str]:
    return [row[3] for row in db.execute(f"EXPLAIN QUERY PLAN {sql}", args)]


def days_ago(count: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=count)


def seed_event(db: sqlite3.Connection, key: str, agent: str | None, observed: datetime, *, ts: datetime | None = None, name: str = "mail.triaged") -> None:
    db.execute(
        "INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,agent_thread_id,attribution,attributes) "
        "VALUES (?,?,?,'agent','agent_recorded',?,?,?,'caller','{}')",
        (key, key, name, (ts or observed).isoformat(), observed.isoformat(), agent),
    )


def test_the_rows_read_is_an_index_range_on_the_agent_and_observed_time() -> None:
    steps = plan(world(), ROWS_SQL, (AGENT, "2026-01-01"))
    assert steps[0].startswith("SEARCH outcome_events USING COVERING INDEX idx_outcome_events_agent_observed (agent_thread_id=? AND observed_ts>?)")
    assert not [step for step in steps if step.startswith("SCAN")]


def test_the_counter_read_is_a_primary_key_range_on_the_agent_and_day() -> None:
    steps = plan(world(), COUNTERS_SQL, (AGENT, "2026-01-01"))
    assert steps[0].startswith("SEARCH outcome_event_counters USING PRIMARY KEY (agent_thread_id=? AND day>?)") or "USING INDEX sqlite_autoindex_outcome_event_counters_1 (agent_thread_id=? AND day>?)" in steps[0]
    assert not [step for step in steps if step.startswith("SCAN")]


def test_the_owner_rows_read_seeks_by_the_primary_key() -> None:
    steps = plan(world(), _OWNER_ROWS, (AGENT,))
    assert not [step for step in steps if step.startswith("SCAN") and "measure_definitions" in step and "PRIMARY KEY" not in step]
    assert any("USING INDEX sqlite_autoindex_measure_definitions_1 (owner_thread_id=?)" in step or "USING PRIMARY KEY (owner_thread_id=?" in step for step in steps)


def test_the_in_transaction_namespace_read_is_an_index_range_on_kind_and_name() -> None:
    steps = plan(world(), NAMESPACE_ROWS, ("qa.", "qa/"))
    assert steps == ["SEARCH measure_definitions USING INDEX idx_measure_definitions_kind_name (kind=? AND name>? AND name<?)"]


def test_status_counts_rows_by_when_they_were_observed_and_only_inside_the_window() -> None:
    db = world()
    seed_event(db, "today", AGENT, days_ago(0))
    seed_event(db, "five-days", AGENT, days_ago(5))
    seed_event(db, "eight-days", AGENT, days_ago(8))
    seed_event(db, "old-source-time", AGENT, days_ago(0), ts=days_ago(300))  # observed today, happened long ago: counted
    seed_event(db, "other-agent", OTHER, days_ago(0))
    seed_event(db, "no-agent", None, days_ago(0))
    for day, n in ((0, 2), (6, 3), (7, 5)):
        db.execute("INSERT INTO outcome_event_counters (counter,event_name,agent_thread_id,day,n) VALUES ('attribute_dropped','mail.triaged',?,?,?)", (AGENT, days_ago(day).date().isoformat(), n))
    db.execute("INSERT INTO outcome_event_counters (counter,event_name,agent_thread_id,day,n) VALUES ('attribute_dropped','mail.triaged',?,?,9)", (OTHER, days_ago(0).date().isoformat()))
    shown = status(db, AGENT)["events"]["mail.triaged"]
    assert shown["today"] == {"rows": 2, "counters": {"attribute_dropped": 2}}
    assert shown["last_7_days"] == {"rows": 3, "counters": {"attribute_dropped": 5}}


def test_synced_files_lists_only_files_that_still_define_something_live() -> None:
    db = world()
    rows = (  # (name, version, removed, file, synced_at)
        ("mail.moved", 1, 0, "/a.yaml", "2026-01-01"),
        ("mail.moved", 2, 0, "/b.yaml", "2026-01-02"),  # moved: /a.yaml no longer defines it
        ("mail.gone", 1, 0, "/c.yaml", "2026-01-01"),
        ("mail.gone", 2, 1, "/c.yaml", "2026-01-03"),  # removed: /c.yaml defines nothing live
    )
    db.executemany(
        "INSERT INTO measure_definitions (owner_thread_id,kind,name,version,spec_json,file_path,file_sha256,synced_at,removed) "
        "VALUES (?,'event',?,?,'{}',?,'sha',?,?)",
        [(AGENT, name, version, file, synced, removed) for name, version, removed, file, synced in rows],
    )
    assert [item["path"] for item in synced_files(db, AGENT)] == ["/b.yaml"]


@pytest.mark.parametrize("sql", [ROWS_SQL, COUNTERS_SQL])
def test_the_window_bound_is_a_plain_parameter_not_a_function_of_the_column(sql: str) -> None:
    assert "substr(" not in sql and "date(" not in sql
