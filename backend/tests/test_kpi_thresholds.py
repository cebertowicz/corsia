"""The daily KPI threshold job wakes the owner of a KPI past its review line, once a day."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import jobs.kpi_thresholds as job
from events.movement_report import MalformedMovementReport, deliver_movement_report_sync
from events.platform import DEAD_LETTERED, DUPLICATE
from infra.conductor import set_project_conductor
from infra.database import get_db
from infra.measure_registry import WorkflowOwner
from jobs.kpi_moves import normal_range
from tests.event_routing_helpers import mk_session
from tests.test_agent_kpi_api import definition, fact
from tests.test_measurement_api import _seed_thread

NOW = datetime(2026, 1, 20, 9, tzinfo=timezone.utc)  # the week of Jan 12 is complete and settled
KPI = {"title": "Escapes", "unit": "count", "better": "lower", "formula": "count",
       "num": {"event": "demo.esc"}, "review": 0, "target": 0, "min_n": 1, "alert": "wake", "settle_days": 1}


@pytest.fixture
def seeded(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr("api.measurement_agent_kpis._repositories", lambda db, project: set())
    monkeypatch.setattr("core.agent_kpi_read.thread_workflow_owners",
                        lambda owner, db: {"demo": WorkflowOwner(tmp_path, "owner")})
    db = get_db()
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('p','P','t','t')")
    _seed_thread(db, "owner", project_id="p", agent=True)
    db.commit()
    definition(db, tmp_path, "event", "demo.esc", {"attributes": {}})
    definition(db, tmp_path, "kpi", "escapes", dict(KPI))
    fact(db, "demo.esc", 2, {"workflow_version": "v1"})   # the first workflow version, used long before the week
    fact(db, "demo.esc", 13, {"workflow_version": "v2"})  # v2 is first used in the evaluated week
    fact(db, "demo.esc", 14, {"workflow_version": "v2"})
    db.commit()
    db.close()
    conductor = mk_session("conductor")
    assert set_project_conductor("p", conductor)
    return tmp_path, conductor


def _count(sql, *args):
    db = get_db()
    try:
        return db.execute(sql, args).fetchone()[0]
    finally:
        db.close()


def _wakes(thread):
    return _count("SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", thread)


def _notes(thread):
    return _count("SELECT COUNT(*) FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?", thread)


def _wake_text(thread):
    return _count("SELECT group_concat(content) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", thread)


def test_breach_wakes_the_owner_once_with_the_preceding_workflow_version(seeded):
    assert job.run_daily(now=NOW) == ["agent:owner:2026-01-20"]
    assert _wakes("owner") == 1
    text = _wake_text("owner")
    assert "kpi:owner:escapes@v1" in text
    assert "demo@v2" in text and "demo@v1" not in text


def test_a_second_run_the_same_day_does_not_wake_again(seeded):
    _, conductor = seeded
    job.run_daily(now=NOW)
    job.run_daily(now=NOW)
    assert _wakes("owner") == 1
    assert _notes(conductor) == 1


def test_the_conductor_is_told_the_move_is_routed_and_not_woken(seeded):
    _, conductor = seeded
    job.run_daily(now=NOW)
    assert _wakes(conductor) == 0
    db = get_db()
    notes = " ".join(row[0] for row in db.execute(
        "SELECT details_json FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?",
        (conductor,)))
    db.close()
    assert "kpi:owner:escapes@v1" in notes and '"owner_agent": "owner"' in notes


def test_a_digest_alert_informs_and_does_not_wake(seeded):
    tmp_path, _ = seeded
    db = get_db()
    db.execute("UPDATE measure_definitions SET spec_json=? WHERE kind='kpi'", (json.dumps({**KPI, "alert": "digest"}),))
    db.commit()
    db.close()
    job.run_daily(now=NOW)
    assert _wakes("owner") == 0
    assert _notes("owner") == 1


def test_a_value_at_the_line_or_inside_the_settle_days_reports_nothing(seeded):
    db = get_db()
    db.execute("UPDATE measure_definitions SET spec_json=? WHERE kind='kpi'", (json.dumps({**KPI, "review": 2}),))
    db.commit()
    db.close()
    assert job.run_daily(now=NOW) == []  # 2 rows is not strictly worse than a review line of 2
    db = get_db()
    db.execute("UPDATE measure_definitions SET spec_json=? WHERE kind='kpi'", (json.dumps({**KPI, "settle_days": 3}),))
    db.commit()
    db.close()
    assert job.run_daily(now=NOW) == []  # the week ended Jan 19; three settle days run to Jan 22
    assert _wakes("owner") == 0


def test_an_owner_made_inactive_before_delivery_is_dead_lettered(seeded, monkeypatch):
    real = job.kpi_moves

    def archive_after_read(*args):
        moves = real(*args)
        db = get_db()
        db.execute("UPDATE threads SET lifecycle_state='archived', archived_reason='user' WHERE id='owner'")
        db.commit()
        db.close()
        return moves

    monkeypatch.setattr(job, "kpi_moves", archive_after_read)
    job.run_daily(now=NOW)
    assert _wakes("owner") == 0
    assert _count("SELECT COUNT(*) FROM dead_letter_events WHERE reason='owner_inactive'") == 1


MOVE = {"series_id": "kpi:ghost:escapes@v1", "owner_agent": "ghost", "value": 2,
        "normal_range": [0, 0], "preceding_changes": []}


def _report(scope, move=MOVE):
    return {"scope": scope, "report_date": "2026-01-20", "breach": True, "moves": [dict(move)]}


def test_an_agent_report_to_a_missing_owner_is_dead_lettered(isolated_db):
    out = deliver_movement_report_sync(_report("agent:ghost"))
    assert [(d.outcome, d.reason) for d in out] == [(DEAD_LETTERED, "owner_inactive")]


def test_a_move_that_is_not_the_reports_agent_is_refused(isolated_db):
    mk_session("someone")
    with pytest.raises(MalformedMovementReport, match="own agent's moves"):
        deliver_movement_report_sync(_report("agent:someone"))
    assert _wakes("someone") == 0


def test_an_agent_report_wakes_the_named_thread_and_repeats_are_noops(isolated_db):
    mk_session("ghost")
    first = deliver_movement_report_sync(_report("agent:ghost"))
    again = deliver_movement_report_sync(_report("agent:ghost"))
    assert [d.thread_id for d in first] == ["ghost"] and _wakes("ghost") == 1
    assert [d.outcome for d in again] == [DUPLICATE]


@pytest.mark.parametrize("better,unit,expected", [
    ("higher", "percent", [0.85, 1]), ("higher", "count", [0.85, None]), ("higher", "minutes", [0.85, None]),
    ("lower", "percent", [0, 0.85]), ("lower", "usd", [0, 0.85]),
])
def test_normal_range_runs_from_the_review_line_to_the_best_bound(better, unit, expected):
    assert normal_range({"review": 0.85, "better": better, "unit": unit}) == expected
