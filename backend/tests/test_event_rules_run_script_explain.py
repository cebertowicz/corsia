"""``explain`` tells a script rule's outcome in its own words: one wording per cause, and a
script that never started never reads as run or failed."""
from __future__ import annotations

import json

from automation import script_schedules
from automation.script_sandbox import ScriptRun
from infra.database import get_db
from tests.test_event_rules_run_script import (  # noqa: F401 - fixtures
    _START, OWNER, _emit, _explained, _outcomes, _rule, _script, _sweep, _tool, _write, runs, world,
)


def test_a_script_that_reports_a_breach_ran_and_is_counted_as_run(runs):
    path = runs["tmp"] / "breach.sh"
    path.write_text("# breach\n")
    db = get_db()
    try:
        script_id = script_schedules.create_script(
            OWNER, "project-a", script_path=path, script_sha256=script_schedules.file_sha256(path),
            cadence_seconds=None, series=script_schedules.parse_series([{"key": "cost", "max": 1}]),
            timeout_seconds=30, now=_START.isoformat(), db=db,
        ).id
    finally:
        db.close()
    rule = _rule(script_id)
    runs["outputs"]["breach.sh"] = ScriptRun(0, b'{"series": "cost", "value": 5}\n', b"", False)
    event_id = _emit()
    _sweep()

    assert _outcomes(rule["id"]) == [("ran", None)]
    assert _explained(event_id) == [("ran", f"rule {rule['id']} ran {OWNER}'s script with it")]
    [listed] = json.loads(_tool(OWNER, operation="list"))["rules"]
    assert (listed["starts_24h"], listed["skips_24h"]) == (1, 0)


def test_an_archived_creator_holds_a_script_rule_without_queueing(runs):
    rule = _rule(_script(runs, "gone.sh"))
    _write("UPDATE threads SET lifecycle_state='archived', archived_reason='user' WHERE id=?", OWNER)
    event_id = _emit()
    _sweep()

    reason = "creator unavailable"
    assert runs["calls"] == [] and _outcomes(rule["id"]) == [("held", reason)]
    assert _explained(event_id) == [
        ("held", f"rule {rule['id']} held it from {OWNER}: {reason}")]
