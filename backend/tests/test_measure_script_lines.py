"""Script output parsing for measure lines: one pass, counted refusals, no_data, the workflow stamp
and its staleness."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from automation import script_runner
from automation.schedules import get as get_schedule
from automation.script_readings import RunReport, build_report, emit_report
from automation.script_sandbox import ScriptRun
from infra.measure_registry import WorkflowOwner
from infra.measure_status import status
from tests.test_measure_intake import GOOD, OTHER, QG, T0, VERSION, counter, rows, schedule, stamp_for, world

LINE = json.dumps({"measure": {"event.name": "qa.review.completed", "attributes": GOOD}})


def emit(db: sqlite3.Connection, sched, stdout: bytes, run_id: str = "uuid-1", now: str = T0) -> RunReport:
    report = dataclasses.replace(build_report(sched, ScriptRun(0, stdout, b"", False), run_id=run_id), workflow_stamp=stamp_for(sched))
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
    assert status(db, QG)["recent_refusals"] == ["5 measure or event line(s) past the 200-line cap were dropped; print fewer per run."]


def test_the_run_result_line_that_automation_list_shows_is_its_own_column(tmp_path: Path) -> None:
    db = world(tmp_path)
    sched = schedule(db, tmp_path)
    emit(db, sched, (LINE + '\n{"measure": 7}\n').encode())
    settled = get_schedule(sched.id, db=db)
    assert settled.last_measures == "measures: 1 recorded, 1 refused (measure_status)"
    assert settled.payload == {"series": [], "workflow": "llm-pr-review"}  # the registration's payload is never patched


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


@pytest.fixture
def owners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """The owner rule's answer, as tools.workflow_owners would give it: llm-pr-review is agent-qg's."""
    from tools import automations, workflow_owners

    answer = {"llm-pr-review": WorkflowOwner(tmp_path, QG)}
    monkeypatch.setattr(workflow_owners, "workflow_owners", lambda *_args: answer)
    monkeypatch.setattr(automations, "get_db", lambda: sqlite3.connect(":memory:"))
    return answer


def test_the_tool_refuses_a_workflow_that_is_unknown_or_not_yours_naming_the_ones_you_own(owners: dict) -> None:
    from tools import automations

    automations._check_workflow(QG, "proj", "llm-pr-review")
    with pytest.raises(ValueError, match=r"^workflow nope is not one of your workflow skills \(yours: llm-pr-review\)\. Name a workflow skill you own\. The schedule was not registered; leave workflow out for agent-level measures\.$"):
        automations._check_workflow(QG, "proj", "nope")
    with pytest.raises(ValueError, match=r"^workflow llm-pr-review is owned by another agent \(thread agent-qg\); only its owner can use it\. The schedule was not registered"):
        automations._check_workflow(OTHER, "proj", "llm-pr-review")


def test_automation_list_reads_workflow_stale_live(owners: dict, tmp_path: Path) -> None:
    from tools import automations

    db = world(tmp_path)
    mine, gone, plain = schedule(db, tmp_path), schedule(db, tmp_path, workflow="retired-flow"), schedule(db, tmp_path, workflow=None)
    listed = {entry["id"]: entry for entry in automations._listed(QG, "proj", [mine, gone, plain])}
    assert listed[mine.id]["workflow_stale"] is False and listed[gone.id]["workflow_stale"] is True
    assert "workflow_stale" not in listed[plain.id]
    owners.clear()  # the skill left the agent's effective skills: the same schedule is now stale
    assert automations._listed(QG, "proj", [mine])[0]["workflow_stale"] is True


def test_the_runner_resolves_the_stamp_before_settling_and_an_unowned_workflow_stamps_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import workflow_owners

    db = world(tmp_path)
    report = build_report(schedule(db, tmp_path), ScriptRun(0, LINE.encode(), b"", False), run_id="uuid-1")
    asked: list[tuple] = []

    def stamp(thread_id: str, workflow: str, _db) -> tuple[str, str] | None:
        asked.append((thread_id, workflow))
        return (workflow, VERSION) if workflow == "llm-pr-review" else None

    monkeypatch.setattr(workflow_owners, "workflow_stamp", stamp)
    monkeypatch.setattr(script_runner, "get_db", lambda: sqlite3.connect(":memory:"))
    stamped = asyncio.run(script_runner._stamped(schedule(db, tmp_path), report))
    assert stamped.workflow_stamp == ("llm-pr-review", VERSION) and asked == [(QG, "llm-pr-review")]
    assert asyncio.run(script_runner._stamped(schedule(db, tmp_path, workflow="retired-flow"), report)).workflow_stamp is None
    assert asyncio.run(script_runner._stamped(schedule(db, tmp_path, workflow=None), report)) is report  # nothing to resolve
    assert len(asked) == 2

    def broken(*_args):
        raise OSError("skills root unreadable")

    monkeypatch.setattr(workflow_owners, "workflow_stamp", broken)
    assert asyncio.run(script_runner._stamped(schedule(db, tmp_path), report)).workflow_stamp is None  # unversioned, not a failed run


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


def test_an_owned_workflow_whose_version_git_cannot_read_records_unversioned_rows(tmp_path: Path) -> None:
    db = world(tmp_path)
    sched = schedule(db, tmp_path)
    report = dataclasses.replace(build_report(sched, ScriptRun(0, LINE.encode(), b"", False), run_id="uuid-1"), workflow_stamp=("llm-pr-review", None))
    emit_report(db, get_schedule(sched.id, db=db), report, now=T0)
    stored = json.loads(rows(db)[0][6])
    assert stored["workflow_ref"] == "llm-pr-review" and "workflow_version" not in stored
    assert counter(db, "unversioned") == 1


def test_a_schedule_with_an_owned_workflow_counts_no_unversioned_rows(tmp_path: Path) -> None:
    db = world(tmp_path)
    emit(db, schedule(db, tmp_path), LINE.encode())
    assert counter(db, "unversioned") == 0 and "unversioned" not in status(db, QG)
