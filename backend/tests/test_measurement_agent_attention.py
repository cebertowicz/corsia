"""The agent-grain attention time behind minutes per outcome on the Agents table."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.measurement_agent_attention import MAX_ACTIVE_GAP_SECONDS, query_root_active
from api.measurement_queries import prepare_outcome_periods, query_family
from core.measurement_scope import resolve_grain

LATEST, PREVIOUS = "2026-09-28", "2026-09-21"
SCOPE = {"scope_project": "all", "scope_agent": None, "user_id": "steve"}
GRAIN = resolve_grain("weeks", "UTC", now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
PARAMS = {**SCOPE, "grain": GRAIN, "max_active_gap_seconds": MAX_ACTIVE_GAP_SECONDS}


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    for thread in ("root", "other"):
        connection.execute("INSERT INTO threads (id,user_id,title,agent_enabled,agent_status,created_at,updated_at) VALUES (?,'steve',?,1,'active','t','t')", (thread, thread))
    try:
        yield connection
    finally:
        connection.close()


def _message(db, message_id: str, thread: str, at: str) -> None:
    db.execute("INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES (?,?, 'user', 'x', '{}', ?)",
               (message_id, thread, at))


def _two_agents_interleaved(db) -> None:
    _message(db, "a1", "root", "2026-09-29T12:00:00Z")
    _message(db, "o1", "other", "2026-09-29T12:02:00Z")  # lands between root's two messages
    _message(db, "a2", "root", "2026-09-29T12:05:00Z")


def _two_agents_taking_turns_past_the_cap(db) -> None:
    # Each agent's own gap is 960 s (over the cap); the person's gaps are 480 s each (under it).
    _message(db, "a1", "root", "2026-09-29T12:00:00Z")
    _message(db, "o1", "other", "2026-09-29T12:08:00Z")
    _message(db, "a2", "root", "2026-09-29T12:16:00Z")


def _agent_rows(db, params):
    prepare_outcome_periods(db, params, roots=True)
    return {row["root_id"]: row["active_hours"] for row in query_root_active(db, params)}


def test_a_gap_is_measured_across_the_whole_stream_of_human_messages(db):
    _two_agents_interleaved(db)
    rows = _agent_rows(db, {**PARAMS})
    # The person's stream is a1, o1, a2: 60 s, then 120 s + 60 s, then 180 s + 60 s.
    assert rows["root"] == pytest.approx(300 / 3600)
    assert rows["other"] == pytest.approx(180 / 3600)


def test_the_agent_rows_add_up_to_the_all_work_figure(db):
    _two_agents_taking_turns_past_the_cap(db)
    params = {**PARAMS}
    rows = _agent_rows(db, params)
    prepare_outcome_periods(db, params)
    total = query_family(db, "active", params)
    assert [row["period"] for row in total] == [LATEST]
    assert total[0]["active_hours"] == pytest.approx(1140 / 3600)
    assert sum(rows.values()) == pytest.approx(total[0]["active_hours"])


def test_an_agent_row_equals_that_agents_own_page(db):
    _two_agents_taking_turns_past_the_cap(db)
    rows = _agent_rows(db, {**PARAMS})
    for agent, hours in rows.items():
        own = {**PARAMS, "scope_agent": agent}
        prepare_outcome_periods(db, own)
        assert query_family(db, "active", own)[0]["active_hours"] == pytest.approx(hours)


def test_the_message_before_the_window_starts_the_first_gap_for_every_agent(db):
    # The window opens 2026-07-13; the earlier message seeds the first gap and is not counted.
    _message(db, "p1", "other", "2026-07-12T23:58:00Z")
    _message(db, "a1", "root", "2026-07-13T00:01:00Z")
    rows = _agent_rows(db, {**PARAMS})
    assert rows == {"root": pytest.approx(240 / 3600)}
