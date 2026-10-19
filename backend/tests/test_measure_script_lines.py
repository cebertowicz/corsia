"""Script output parsing for measure lines: one pass, counted refusals, no_data, workflow staleness."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from automation.schedules import get as get_schedule
from automation.script_readings import RunReport, build_report, emit_report
from automation.script_sandbox import ScriptRun
from infra.measure_admission import workflow_refusal
from infra.measure_status import status
from tests.test_measure_intake import GOOD, OTHER, QG, T0, counter, rows, schedule, world

LINE = json.dumps({"measure": {"event.name": "qa.review.completed", "attributes": GOOD}})


def emit(db: sqlite3.Connection, sched, stdout: bytes, run_id: str = "uuid-1", now: str = T0) -> RunReport:
    report = build_report(sched, ScriptRun(0, stdout, b"", False), run_id=run_id)
    emit_report(db, get_schedule(sched.id, db=db), report, now=now)
    return report


def wakes(db: sqlite3.Connection) -> list[str]:
    return [row[0] for row in db.execute("SELECT content FROM messages WHERE role='system' AND content LIKE '[EVENT: script.%' ORDER BY rowid")]


def test_one_pass_returns_readings_and_measure_lines_together(tmp_path: Path) -> None:
    db = world(tmp_path)
    sched = schedule(db, tmp_path)
    object.__setattr__(sched, "payload", {"series": [{"key": "cost", "unit": "usd", "min": None, "max": None, "worse": None}], "workflow": "llm-pr-review"})
    report = build_report(sched, ScriptRun(0, ('{"series":"cost","value":2}\n' + LINE + "\n").encode(), b"", False), run_id="uuid-1")
    assert [(r.series, r.value) for r in report.readings] == [("cost", 2.0)]
    assert len(report.measures) == 1 and report.measure_refusals == [] and report.error is None


def test_output_that_is_not_utf8_admits_nothing_and_is_a_run_error(tmp_path: Path) -> None:
    db = world(tmp_path)
    sched = schedule(db, tmp_path)
    report = emit(db, sched, LINE.encode() + b"\n\xff\xfe\n")
    assert report.status == "error" and report.measures == [] and "not valid UTF-8" in (report.error or "")
    assert rows(db) == []


def test_a_line_with_an_unknown_top_level_key_stays_a_run_error(tmp_path: Path) -> None:
    db = world(tmp_path)
    report = emit(db, schedule(db, tmp_path), b'{"metric": 1}\n' + LINE.encode() + b"\n")
    assert report.status == "error" and "line 1" in (report.error or "")
    assert len(rows(db)) == 1  # the good measure line still lands


@pytest.mark.parametrize("bad", [
    {"measure": 7},
    {"measure": {"attributes": {}}},
    {"measure": {"event.name": 5, "attributes": {}}},
    {"measure": {"event.name": "qa.review.completed", "attributes": [1]}},
    {"measure": {"event.name": "qa.review.completed", "attributes": GOOD, "time": 1767268800}},
])
def test_malformed_measure_lines_are_refused_counted_and_never_fail_the_run(tmp_path: Path, bad: dict) -> None:
    db = world(tmp_path)
    report = emit(db, schedule(db, tmp_path), (json.dumps(bad) + "\n" + LINE + "\n").encode())
    assert report.error is None and report.status == "ok" and len(report.measure_refusals) == 1
    assert counter(db, "record_refused") == 1 and len(rows(db)) == 1
    shown = status(db, QG)["recent_refusals"]
    assert len(shown) == 1 and shown[0].startswith("measure line 1: ") and shown[0].endswith("the line was dropped.")
    assert wakes(db) == []


def test_lines_past_the_cap_get_one_counted_note_with_the_number_dropped(tmp_path: Path) -> None:
    db = world(tmp_path)
    stdout = "".join(json.dumps({"measure": {"event.name": "qa.review.completed", "attributes": {**GOOD, "pr": str(n)}}}) + "\n" for n in range(205))
    report = emit(db, schedule(db, tmp_path), stdout.encode())
    assert report.measures_over_cap == 5 and len(rows(db)) == 200
    assert counter(db, "record_refused") == 5
    assert status(db, QG)["recent_refusals"] == ["5 measure line(s) past the 200-line cap were dropped; print fewer per run."]


def test_the_run_result_line_that_automation_list_shows(tmp_path: Path) -> None:
    db = world(tmp_path)
    sched = schedule(db, tmp_path)
    emit(db, sched, (LINE + '\n{"measure": 7}\n').encode())
    payload = get_schedule(sched.id, db=db).payload
    assert payload["last_measures"] == "measures: 1 recorded, 1 refused (measure_status)"
    assert payload["workflow"] == "llm-pr-review" and payload["workflow_stale"] is False


def test_a_run_with_no_reading_and_no_accepted_line_is_no_data_once_a_day(tmp_path: Path) -> None:
    db = world(tmp_path)
    sched = schedule(db, tmp_path)  # series: []
    emit(db, sched, b"")
    assert get_schedule(sched.id, db=db).last_status == "no_data"
    emit(db, sched, b"", run_id="uuid-2")
    assert len(wakes(db)) == 1  # the second run the same day is a note, not another wake
    emit(db, sched, ('{"measure": 7}\n').encode(), run_id="uuid-3")
    assert get_schedule(sched.id, db=db).last_status == "no_data"  # a refused line is not an accepted one


def test_a_run_with_one_accepted_measure_line_settles_ok(tmp_path: Path) -> None:
    db = world(tmp_path)
    sched = schedule(db, tmp_path)
    emit(db, sched, LINE.encode())
    assert get_schedule(sched.id, db=db).last_status == "ok" and wakes(db) == []


def test_build_report_without_a_run_id_fails_loudly(tmp_path: Path) -> None:
    db = world(tmp_path)
    with pytest.raises(TypeError, match="run_id"):
        build_report(schedule(db, tmp_path), ScriptRun(0, b"", b"", False))  # type: ignore[call-arg]


def test_a_report_with_measure_lines_but_no_run_id_is_refused_at_settle(tmp_path: Path) -> None:
    db = world(tmp_path)
    sched = schedule(db, tmp_path)
    report = RunReport("ok", [], [], measures=[json.loads(LINE)["measure"]])
    with pytest.raises(ValueError, match="run_id"):
        emit_report(db, get_schedule(sched.id, db=db), report, now=T0)


def test_registering_a_workflow_that_is_unknown_or_not_yours_names_the_ones_you_own(tmp_path: Path) -> None:
    db = world(tmp_path)
    assert workflow_refusal(db, QG, "llm-pr-review") is None
    assert workflow_refusal(db, QG, "nope") == 'workflow "nope" is not declared by any synced measures.yaml; the schedule was not registered. Use a workflow you own (yours: llm-pr-review), or leave workflow out.'
    assert workflow_refusal(db, OTHER, "llm-pr-review") == 'workflow "llm-pr-review" is owned by another agent; the schedule was not registered. Use a workflow you own (yours: none), or leave workflow out.'


def test_the_tool_refuses_a_bad_workflow_before_it_registers_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import automations

    db = world(tmp_path)

    class Held:
        def __getattr__(self, name: str):
            return (lambda: None) if name == "close" else getattr(db, name)

    monkeypatch.setattr(automations, "get_db", lambda: Held())
    automations._check_workflow(QG, "llm-pr-review")
    with pytest.raises(ValueError, match="not declared by any synced measures.yaml"):
        automations._check_workflow(QG, "nope")
    with pytest.raises(ValueError, match="owned by another agent"):
        automations._check_workflow(OTHER, "llm-pr-review")


def test_rows_from_a_schedule_whose_workflow_is_no_longer_owned_are_unversioned_and_counted(tmp_path: Path) -> None:
    db = world(tmp_path)
    sched = schedule(db, tmp_path, workflow="retired-flow")
    report = emit(db, sched, LINE.encode())
    assert report.error is None and get_schedule(sched.id, db=db).last_status == "ok"  # the run neither fails nor wakes
    assert wakes(db) == []
    stored = json.loads(rows(db)[0][6])
    assert "workflow_ref" not in stored and stored["run_id"] == f"script:{sched.id}:uuid-1"
    assert counter(db, "unversioned") == 1
    shown = status(db, QG)
    assert shown["events"]["qa.review.completed"]["today"]["counters"] == {"unversioned": 1}
    assert shown["unversioned"].startswith("1 rows unversioned")
    assert get_schedule(sched.id, db=db).payload["workflow_stale"] is True


def test_a_schedule_with_an_owned_workflow_counts_no_unversioned_rows(tmp_path: Path) -> None:
    db = world(tmp_path)
    emit(db, schedule(db, tmp_path), LINE.encode())
    assert counter(db, "unversioned") == 0 and "unversioned" not in status(db, QG)
