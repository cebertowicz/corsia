"""``run_script`` rules: a matching event runs its writer's script once, through the real
intake, evaluation, script runner and settlement. Only the sandboxed process is faked."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from automation import script_runner, script_schedules
from automation.script_sandbox import ScriptRun
from infra.database import get_db
from infra.measure_registry import REGISTRY, sync
from tests.event_routing_helpers import mk_session
from tools.event_rules import EventRulesTool

OWNER, OTHER = "run-owner", "run-other"
#: Schedules register after every sweep below, so only rule runs start.
_LATER = "2099-01-01T00:00:00+00:00"

_DECLARATIONS = """schema: corsia.measures/1
namespace: qa
events:
  qa.route:
    attributes: {code: id}
    dedupe: [code]
    use: route
  qa.two:
    attributes: {code: id}
    dedupe: [code]
    use: route
  qa.three:
    attributes: {code: id}
    dedupe: [code]
    use: route
"""


@pytest.fixture
def world(isolated_db, tmp_path: Path, monkeypatch):
    REGISTRY.reset()
    script_runner._in_flight.clear()
    mk_session(OWNER, project_id="project-a")
    mk_session(OTHER, project_id="project-a")
    folder = tmp_path / ".threads" / OWNER
    folder.mkdir(parents=True)
    path = folder / "measures.yaml"
    path.write_text(_DECLARATIONS)
    db = get_db()
    sync(db, owner_thread_id=OWNER, path=str(path), text=_DECLARATIONS)
    db.commit()
    db.close()
    driven: list[str] = []

    async def drive_owner(thread_id):
        driven.append(thread_id)

    monkeypatch.setattr(script_runner, "_drive_owner", drive_owner)
    return {"tmp": tmp_path, "driven": driven, "outputs": {}, "calls": []}


@pytest.fixture
def runs(world, monkeypatch):
    """The sandboxed process: each script's stdout by file name, and every call's stdin."""

    async def fake(thread_id, project_id, path, *, run_id, timeout_seconds, stdin=None):
        world["calls"].append((path.name, json.loads(stdin) if stdin is not None else None))
        return world["outputs"].get(path.name, ScriptRun(0, b"", b"", False))

    monkeypatch.setattr(script_runner, "run_script", fake)
    return world


def _tool(thread: str, **kwargs) -> str:
    return asyncio.run(EventRulesTool().execute(_thread_id=thread, **kwargs))


def _script(world, name: str, owner: str = OWNER) -> str:
    path = world["tmp"] / name
    path.write_text(f"# {name}\n")
    db = get_db()
    try:
        return script_schedules.create_script(
            owner, "project-a", script_path=path, script_sha256=script_schedules.file_sha256(path),
            cadence_seconds=600, series=[], timeout_seconds=30, now=_LATER, db=db,
        ).id
    finally:
        db.close()


def _rule(script_id: str, event_type: str = "route", **kwargs) -> dict:
    return json.loads(_tool(OWNER, operation="create", connector="qa", event_type=event_type,
                            action="run_script", script_id=script_id,
                            match=[{"field": "code", "op": "present"}], **kwargs))


def _emit(event_type: str = "qa.route", code: str = "ok") -> str:
    return json.loads(_tool(OWNER, operation="emit", event_type=event_type, facts={"code": code}))["event_id"]


def _emits(*lines: tuple[str, str]) -> ScriptRun:
    out = "".join(json.dumps({"event": {"type": t, "facts": {"code": c}}}) + "\n" for t, c in lines)
    return ScriptRun(0, out.encode(), b"", False)


def _sweep() -> None:
    """Sweep until no run starts, settling each sweep's runs before the next."""

    async def go():
        now = datetime.now(timezone.utc).isoformat()
        while await script_runner.drive(now):
            await asyncio.gather(*script_runner._in_flight.values())

    asyncio.run(go())


def _outcomes(rule_id: str) -> list[tuple]:
    db = get_db()
    try:
        return [tuple(row) for row in db.execute(
            "SELECT outcome, reason FROM event_route_decisions WHERE rule_id=? ORDER BY id", (rule_id,))]
    finally:
        db.close()


def _digested(thread: str, event_id: str) -> int:
    db = get_db()
    try:
        return db.execute(
            "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='thread_context' AND object_id=? "
            "AND json_extract(details_json,'$.event_id')=?", (thread, event_id)).fetchone()[0]
    finally:
        db.close()


# 1, 3, 4 -- one run with the event's facts, shown by explain and counted by list.
def test_a_matching_rule_runs_the_script_once_with_the_event_and_explain_and_list_show_it(runs):
    rule = _rule(_script(runs, "once.sh"))
    event_id = _emit(code="ok")
    _sweep()
    _sweep()  # nothing is left to run

    assert runs["calls"] == [("once.sh", {"type": "qa.route", "id": event_id, "rule_id": rule["id"],
                                           "facts": {"code": "ok"}})]
    assert _outcomes(rule["id"]) == [("ran", None)]
    explained = json.loads(_tool(OWNER, operation="explain", event_id=event_id))["decisions"]
    assert [(d["outcome"], d["explanation"]) for d in explained] == [
        ("ran", f"rule {rule['id']} ran {OWNER}'s script with it")]
    [listed] = json.loads(_tool(OWNER, operation="list"))["rules"]
    assert (listed["script_id"], listed["starts_24h"], listed["wakes_24h"]) == (rule["script_id"], 1, 0)
    assert listed["last_fired"] is not None
    assert _digested(OWNER, event_id) == 0  # a run_script rule doesn't bring the sender's echo back


# 2 -- the run's event lines go through the intake.
def test_the_runs_event_lines_are_admitted(runs):
    _rule(_script(runs, "emits.sh"))
    runs["outputs"]["emits.sh"] = _emits(("qa.two", "a"), ("qa.three", "b"))
    _emit()
    _sweep()
    db = get_db()
    try:
        admitted = [tuple(row) for row in db.execute(
            "SELECT event_type, outcome FROM event_intake_records WHERE producer LIKE 'script:%' ORDER BY id")]
    finally:
        db.close()
    assert admitted == [("qa.two", "accepted"), ("qa.three", "accepted")]


# 5 -- a paused owner's run is skipped and never starts.
def test_a_paused_owner_gives_skipped_owner_paused_and_no_run(runs, monkeypatch):
    rule = _rule(_script(runs, "paused.sh"))
    _emit()
    monkeypatch.setattr("plans.public.thread_paused_since",
                        lambda _db, thread: "2026-01-01T00:00:00+00:00" if thread == OWNER else None)
    _sweep()
    assert runs["calls"] == [] and _outcomes(rule["id"]) == [("skipped", "owner paused")]


# 6 -- a script that emits what its own rule matches runs once, then the loop guard holds.
def test_a_self_loop_runs_once_then_records_skipped_loop(runs):
    rule = _rule(_script(runs, "loop.sh"))
    runs["outputs"]["loop.sh"] = _emits(("qa.route", "again"))
    _emit()
    _sweep()
    assert [name for name, _ in runs["calls"]] == ["loop.sh"]
    assert _outcomes(rule["id"]) == [("ran", None), ("skipped", "loop")]


# 7 -- distinct rules chain freely.
def test_a_chain_of_three_distinct_rules_all_run(runs):
    first = _rule(_script(runs, "one.sh"))
    second = _rule(_script(runs, "two.sh"), event_type="two")
    third = _rule(_script(runs, "three.sh"), event_type="three")
    runs["outputs"]["one.sh"] = _emits(("qa.two", "x"))
    runs["outputs"]["two.sh"] = _emits(("qa.three", "y"))
    _emit()
    _sweep()
    assert [(name, event["type"]) for name, event in runs["calls"]] == [
        ("one.sh", "qa.route"), ("two.sh", "qa.two"), ("three.sh", "qa.three")]
    assert [_outcomes(rule["id"]) for rule in (first, second, third)] == [[("ran", None)]] * 3


# 8 -- a failed run reaches its owner the way a failed scheduled run does.
def test_a_failed_run_reports_to_its_owner_through_the_existing_failure_path(runs):
    script_id = _script(runs, "fails.sh")
    rule = _rule(script_id)
    runs["outputs"]["fails.sh"] = ScriptRun(3, b"", b"boom", False)
    _emit()
    _sweep()
    db = get_db()
    try:
        row = db.execute("SELECT last_status, last_error, running_since, next_run_at FROM automation_schedules "
                         "WHERE id=?", (script_id,)).fetchone()
    finally:
        db.close()
    [(outcome, reason)] = _outcomes(rule["id"])
    assert outcome == "failed" and reason == row["last_error"] and "boom" in reason
    assert (row["last_status"], row["running_since"], row["next_run_at"]) == ("error", None, _LATER)
    assert runs["driven"] == [OWNER]


# 9 -- create refuses what a run_script rule can't run.
def test_create_refuses_an_unknown_or_foreign_script_and_other_actions_fields(runs):
    foreign = _script(runs, "theirs.sh", owner=OTHER)
    mine = _script(runs, "mine.sh")

    def create(**kwargs) -> str:
        return _tool(OWNER, operation="create", connector="qa", event_type="route", action="run_script",
                     **kwargs)

    assert create(script_id="no-such-script").startswith("Refused: no-such-script isn't one of your")
    assert create(script_id=foreign).startswith(f"Refused: {foreign} isn't one of your")
    for extra in ({"to": "self"}, {"mode": "wake"}, {"note": "do it"}):
        assert create(script_id=mine, **extra).startswith("Refused: A run_script rule takes no")
    assert _tool(OWNER, operation="create", connector="qa", event_type="route",
                 script_id=mine).startswith("Refused: script_id goes only with action run_script")
    assert json.loads(_tool(OWNER, operation="list"))["rules"] == []


# 10 -- F6: an own emit doesn't echo into its sender's digest unless a deliver rule asks for it.
def test_an_own_emit_skips_the_senders_digest_unless_its_deliver_rule_matches(runs):
    unmatched = _emit(code="before")
    _tool(OWNER, operation="create", connector="qa", event_type="route", mode="digest",
          match=[{"field": "code", "op": "eq", "value": "wanted"}])
    matched = _emit(code="wanted")
    assert (_digested(OWNER, unmatched), _digested(OWNER, matched)) == (0, 1)


# 11 -- the writer's own drop stops its run.
def test_a_drop_rule_stops_a_run_script_rule(runs):
    rule = _rule(_script(runs, "dropped.sh"))
    drop = json.loads(_tool(OWNER, operation="create", connector="qa", event_type="route", action="drop"))
    _emit()
    _sweep()
    assert runs["calls"] == []
    assert _outcomes(rule["id"]) == [("dropped", f"dropped by rule {drop['id']}")]
