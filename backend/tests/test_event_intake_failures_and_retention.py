"""Declared-event intake: one declaration, surface-neutral fixes, failure split, retention, receipts."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from automation import schedules, script_runner, script_schedules
from automation.script_readings import RunReport
from events import intake, intake_errors, rule_declarations, rules
from infra.database import get_db
from tests.test_event_emit import emit_world  # noqa: F401 - fixture
from tests.test_event_intake import OWNER, _admit, intake_world  # noqa: F401 - fixture
from tools.event_rules import EventRulesTool


def _schedule(db, tmp_path):
    path = tmp_path / "events.py"
    path.write_text("print('event')")
    return script_schedules.create_script(
        OWNER, "project", script_path=path, script_sha256=script_schedules.file_sha256(path),
        cadence_seconds=600, series=[], timeout_seconds=30,
        now="2026-01-01T00:00:00+00:00", db=db,
    )


def _raising(exc: Exception):
    async def admit(**_kwargs):
        raise exc
    return admit


@pytest.mark.parametrize(("exc", "code", "text"), [
    (RuntimeError("boom"), "internal_error", "the event intake failed unexpectedly"),
    (sqlite3.OperationalError("database is locked"), "intake_unreachable", "event intake is unavailable"),
])
def test_an_intake_exception_on_an_event_line_settles_the_run_as_an_owner_error(
    intake_world, tmp_path, monkeypatch, exc, code, text,
):
    schedule = _schedule(intake_world, tmp_path)
    monkeypatch.setattr("events.intake.admit_event", _raising(exc))
    report = asyncio.run(script_runner._admit_events(
        schedule, RunReport("ok", [], [], events=[{"type": "qa.route", "facts": {"code": "ok"}}], run_id="r1"),
    ))

    # A script can't resend a line by id (the runner builds it), so its outage says to run it again.
    fix = script_runner._RERUN_FIX if code == "intake_unreachable" else None
    message = f"event line refused: {intake_errors.format_event_error(code, text, fix)}"
    assert "same event id" not in message
    assert report.status == "error" and report.error == message
    assert [signal.kind for signal in report.signals] == ["error"] and message in report.signals[0].summary


def test_run_claimed_returns_and_records_an_error_when_the_intake_raises(intake_world, tmp_path, monkeypatch):
    schedule = _schedule(intake_world, tmp_path)

    async def execute(_schedule):
        return RunReport("ok", [], [], events=[{"type": "qa.route", "facts": {"code": "ok"}}], run_id="r1")

    async def no_drive(_thread_id):
        return None

    async def in_test_db(_owned, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(script_runner, "_execute", execute)
    monkeypatch.setattr(script_runner, "_drive_owner", no_drive)
    monkeypatch.setattr(script_runner, "run_in_db_unless_owned", in_test_db)
    monkeypatch.setattr("events.intake.admit_event", _raising(RuntimeError("boom")))

    asyncio.run(script_runner.run_claimed(schedule, "2026-01-01T00:00:00+00:00", db=intake_world))

    assert schedules.get(schedule.id, db=intake_world).last_status == "error"


def test_rules_and_intake_read_one_declaration(emit_world):  # noqa: F811
    db = get_db()
    try:
        by_rule, entry = rule_declarations.declared_event(db, "qa", "route")
        by_intake, intake_entry = intake.resolve_event_type(db, "qa.route")
    finally:
        db.close()

    assert by_rule == by_intake and entry is intake_entry
    assert by_rule.filterable_fields == tuple(entry.declared)


@pytest.mark.parametrize(("exc", "expected"), [
    (RuntimeError("boom"), "internal_error"),
    (sqlite3.OperationalError("no such table: x"), "internal_error"),
    (sqlite3.OperationalError("database is locked"), "intake_unreachable"),
    (OSError("disk gone"), "intake_unreachable"),
])
def test_failure_code_splits_unreachable_from_a_defect(exc, expected):
    assert intake_errors.failure_code(exc) == expected


def test_agent_emit_reports_a_defect_as_internal_error_and_an_outage_as_retryable(emit_world, monkeypatch):  # noqa: F811
    tool = EventRulesTool()
    run = lambda: asyncio.run(tool.execute(  # noqa: E731
        operation="emit", event_type="qa.route", facts={"code": "ok"}, _thread_id="emit-owner",
    ))
    monkeypatch.setattr("events.intake.admit_event", _raising(RuntimeError("boom")))
    defect = run()
    monkeypatch.setattr("events.intake.admit_event", _raising(sqlite3.OperationalError("database is locked")))
    outage = run()

    assert defect.startswith("Error: internal_error:") and "report it instead of retrying" in defect
    assert outage.startswith("Error: intake_unreachable:") and "Retry with the same event id." in outage


def test_error_fixes_name_no_surface_flags_and_point_at_the_declaration_surface():
    for code in intake_errors._ERROR_FIXES:
        assert "--" not in intake_errors.format_event_error(code, "text"), code
    for code in ("undeclared_type", "not_your_namespace"):
        fix = intake_errors.format_event_error(code, "text")
        assert "measures.yaml" in fix and "measure_sync" in fix and "use: route or both" in fix


def _seed(db, event_id: str, outcome: str, age: timedelta) -> None:
    at = (datetime.now(timezone.utc) - age).isoformat()
    db.execute(
        "INSERT INTO event_intake_records (event_id,outcome,rules_matched,refusal_code,event_type,at) "
        "VALUES (?,?,0,?,'qa.route',?)",
        (event_id, outcome, "undeclared_fact" if outcome == "refused" else None, at),
    )
    db.commit()


def _ids(db) -> set[str]:
    return {row[0] for row in db.execute("SELECT event_id FROM event_intake_records")}


def test_intake_records_of_both_outcomes_age_out_at_the_rule_retention(intake_world):
    old, recent = rules.RETENTION + timedelta(days=1), timedelta(days=1)
    for name, outcome, age in (("old-ok", "accepted", old), ("old-bad", "refused", old),
                               ("new-ok", "accepted", recent), ("new-bad", "refused", recent)):
        _seed(intake_world, name, outcome, age)

    assert _admit(event_id="fresh-1").accepted
    assert _ids(intake_world) == {"new-ok", "new-bad", "fresh-1"}

    _seed(intake_world, "old-ok-2", "accepted", old)
    _seed(intake_world, "old-bad-2", "refused", old)
    intake.record_refusal(
        event_id="fresh-2", event_type="qa.route", origin="emitter", producer=OWNER,
        refusal=intake.IntakeRefusal("undeclared_fact", "text"),
    )
    assert _ids(intake_world) == {"new-ok", "new-bad", "fresh-1", "fresh-2"}


def test_the_intake_table_has_an_index_for_the_refusal_read(intake_world):
    columns = [
        [row[2] for row in intake_world.execute(f"PRAGMA index_info({index[1]})")]
        for index in intake_world.execute("PRAGMA index_list(event_intake_records)")
    ]
    assert ["outcome", "event_type", "at"] in columns


def test_a_first_time_event_with_no_deliveries_is_not_a_duplicate(intake_world, monkeypatch):
    monkeypatch.setattr(intake, "deliver_envelope_sync", lambda *_args, **_kwargs: ([], None))
    first = _admit(event_id="untargeted")
    assert first.accepted and first.duplicate is False
    assert _admit(event_id="untargeted").duplicate is True  # the receipt, not the deliveries, marks a retry
