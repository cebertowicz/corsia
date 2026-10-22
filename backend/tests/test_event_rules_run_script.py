"""``run_script`` rules: a matching event runs its writer's script once, through the real
intake, evaluation, script runner and settlement. Only the sandboxed process is faked."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from automation import script_runner, script_schedules
from automation.script_sandbox import ScriptRun
from infra.database import get_db
from infra.measure_registry import REGISTRY, sync
from tests.event_routing_helpers import mk_session
from tools.event_rules import EventRulesTool

OWNER, OTHER = "run-owner", "run-other"
_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

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
    return {"tmp": tmp_path, "driven": driven, "outputs": {}, "calls": [], "traces": []}


@pytest.fixture
def runs(world, monkeypatch):
    """The sandboxed process: each script's stdout by file name, and every call's stdin."""

    async def fake(thread_id, project_id, path, *, schedule_id, run_id, timeout_seconds, stdin=None, trace=()):
        world["calls"].append((path.name, json.loads(stdin) if stdin is not None else None))
        world["traces"].append(trace)
        return world["outputs"].get(path.name, ScriptRun(0, b"", b"", False))

    monkeypatch.setattr(script_runner, "run_script", fake)
    return world


def _tool(thread: str, **kwargs) -> str:
    return asyncio.run(EventRulesTool().execute(_thread_id=thread, **kwargs))


def _script(world, name: str, owner: str = OWNER, cadence: int | None = None) -> str:
    """A script schedule; with no cadence only rules start it."""
    path = world["tmp"] / name
    path.write_text(f"# {name}\n")
    db = get_db()
    try:
        return script_schedules.create_script(
            owner, "project-a", script_path=path, script_sha256=script_schedules.file_sha256(path),
            cadence_seconds=cadence, series=[], timeout_seconds=30, now=_START.isoformat(), db=db,
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


def _sweep(at: datetime | None = None) -> None:
    """Sweep until no run starts, settling each sweep's runs before the next."""

    async def go():
        now = (at or datetime.now(timezone.utc)).isoformat()
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


def _explained(event_id: str) -> list[tuple[str, str]]:
    decisions = json.loads(_tool(OWNER, operation="explain", event_id=event_id))["decisions"]
    return [(d["outcome"], d["explanation"]) for d in decisions]


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
    assert (row["last_status"], row["running_since"]) == ("error", None)
    assert runs["driven"] == [OWNER]


# A script with no cadence runs only from rules; a cadence script with a rule runs both ways.
def test_a_rules_only_script_never_runs_on_the_sweep_and_a_cadence_script_runs_both_ways(runs):
    from automation.schedules import get
    from tools.automations import _listed

    rules_only = _script(runs, "events-only.sh")
    both = _script(runs, "both.sh", cadence=86400)
    _rule(rules_only)
    _rule(both)
    for day in range(8):  # eight simulated days of hourly sweeps
        for hour in range(24):
            _sweep(_START + timedelta(days=day, hours=hour))
    assert [name for name, _ in runs["calls"]] == ["both.sh"] * 8
    assert all(stdin is None for _, stdin in runs["calls"])
    db = get_db()
    try:
        row = db.execute("SELECT runs, last_status, next_run_at FROM automation_schedules WHERE id=?",
                         (rules_only,)).fetchone()
        [listed] = _listed(OWNER, "project-a", [get(rules_only, db=db)])
    finally:
        db.close()
    assert (row["runs"], row["last_status"]) == (0, None)  # never ran, so never reported no data
    assert listed["next_run_at"] is None and listed["runs_from"].startswith("event rules only")

    runs["calls"].clear()
    event_id = _emit()
    _sweep(_START + timedelta(days=8, minutes=30))  # day 8's slot is due as well
    assert sorted((name, stdin["id"] if stdin else "") for name, stdin in runs["calls"]) == [
        ("both.sh", ""), ("both.sh", event_id), ("events-only.sh", event_id)]


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


# 11 -- the writer's own drop stops its run.
def test_a_drop_rule_stops_a_run_script_rule(runs):
    rule = _rule(_script(runs, "dropped.sh"))
    drop = json.loads(_tool(OWNER, operation="create", connector="qa", event_type="route", action="drop"))
    _emit()
    _sweep()
    assert runs["calls"] == []
    assert _outcomes(rule["id"]) == [("dropped", f"dropped by rule {drop['id']}")]


# The guards around a run: the loop guard through the CLI (A), the bounded queue (B), one run
# of a script at a time (C), no stored next run for a rules-only script (D) and truthful skips (E).

def _cli_emit(capsys, monkeypatch, trace: str | None, code: str = "cli") -> tuple[int, str]:
    import cli as corsia_cli

    if trace is not None:
        monkeypatch.setenv("CORSIA_EVENT_TRACE", trace)
    try:
        rc = corsia_cli.main(["event", "emit", "qa.route", "--facts", json.dumps({"code": code}),
                              "--as-session", OWNER])
    except SystemExit as exc:
        rc = exc.code
    return rc, capsys.readouterr().err


def _write(sql: str, *args) -> None:
    db = get_db()
    try:
        db.execute(sql, args)
        db.commit()
    finally:
        db.close()


def test_a_rule_run_hands_its_chain_to_the_script_and_a_cli_emit_with_it_is_skipped_loop(
        runs, capsys, monkeypatch):
    from events.trace_env import encode

    rule = _rule(_script(runs, "cli-loop.sh"))
    _emit()
    _sweep()
    assert runs["traces"] == [(rule["id"],)]  # the runner passes the run's chain to the sandbox
    rc, err = _cli_emit(capsys, monkeypatch, encode(runs["traces"][0]))
    _sweep()
    assert (rc, err) == (0, "")
    assert len(runs["calls"]) == 1 and _outcomes(rule["id"]) == [("ran", None), ("skipped", "loop")]


@pytest.mark.parametrize("raw", ['{"not": "a list"}', "[1]", json.dumps(["r" * 65]), "not json",
                                 json.dumps(["r"] * 101)])
def test_a_malformed_chain_is_a_usage_error_and_nothing_is_admitted(runs, capsys, monkeypatch, raw):
    rule = _rule(_script(runs, "bad-trace.sh"))
    rc, err = _cli_emit(capsys, monkeypatch, raw)
    assert rc == 2 and err.count("\n") == 1
    assert err.startswith("error: usage: CORSIA_EVENT_TRACE must be a JSON list")
    assert _outcomes(rule["id"]) == []


def test_the_sandbox_sets_the_chain_only_for_a_rule_run_and_never_inherits_one(monkeypatch, tmp_path):
    from automation import script_sandbox
    from host_access.processes import ForegroundProcessResult

    envs: list[dict] = []

    async def nothing(*_a, **_k):
        return None

    async def cwd(*_a, **_k):
        return str(tmp_path), None

    async def prepare(*_a):
        (tmp_path / "profile").write_text("")
        return tmp_path / "profile", ["true"]

    async def spawn(_argv, **kwargs):
        envs.append(kwargs["env"])
        return ForegroundProcessResult(None, b"", b"", 0)

    monkeypatch.setenv("CORSIA_EVENT_TRACE", '["inherited"]')
    monkeypatch.setenv("CORSIA_EVENT_SCRIPT", "inherited")
    monkeypatch.setattr(script_sandbox, "_checked_environment", nothing)
    monkeypatch.setattr(script_sandbox, "load_shell_repo_policies", lambda *_a: ([], []))
    monkeypatch.setattr(script_sandbox, "_resolve_execution_cwd", cwd)
    monkeypatch.setattr(script_sandbox, "execution_envelope_for_consumed_action", lambda *_a, **_k: None)
    monkeypatch.setattr(script_sandbox, "prepare_execution_envelope", prepare)
    monkeypatch.setattr(script_sandbox, "corsia_temp_root_for_bind", lambda _thread: tmp_path)
    monkeypatch.setattr(script_sandbox.PROCESS_OWNER, "spawn", spawn)
    script = tmp_path / "s.sh"
    for kwargs in ({}, {"stdin": b"{}", "trace": ("rule_a", "rule_b")}):
        asyncio.run(script_sandbox.run_script(OWNER, None, script, schedule_id="sched", run_id="r",
                                               timeout_seconds=5, **kwargs))
    assert [env.get("CORSIA_EVENT_TRACE") for env in envs] == [None, '["rule_a", "rule_b"]']
    assert [env.get("CORSIA_EVENT_SCRIPT") for env in envs] == ["sched", "sched"]


def test_a_rules_queue_is_capped_and_a_skip_shows_in_explain_and_list(runs, monkeypatch):
    from events import rule_runs

    monkeypatch.setattr(rule_runs, "QUEUE_CAP", 2)
    rule = _rule(_script(runs, "capped.sh"))
    events = [_emit(code=f"c{i}") for i in range(3)]
    assert _outcomes(rule["id"]) == [("queued", None), ("queued", None), ("skipped", "queue full")]
    assert _explained(events[2]) == [("skipped", f"rule {rule['id']} did not run {OWNER}'s script: queue full")]
    [listed] = json.loads(_tool(OWNER, operation="list"))["rules"]
    assert (listed["starts_24h"], listed["skips_24h"]) == (2, 1)


def test_a_sweep_reads_only_its_free_capacity_of_the_queue(runs, monkeypatch):
    from events import rule_runs

    rules = [_rule(_script(runs, f"many-{i}.sh")) for i in range(6)]
    _emit()  # one event, six rules: six runs queued
    reads: list[tuple[int, list[str]]] = []
    real = rule_runs.queued

    def spy(db, limit):
        rows = real(db, limit)
        reads.append((limit, sorted(rows[0].keys()) if rows else []))
        return rows

    monkeypatch.setattr(rule_runs, "queued", spy)

    async def once():
        started = await script_runner.drive(datetime.now(timezone.utc).isoformat())
        await asyncio.gather(*script_runner._in_flight.values())
        return started

    assert asyncio.run(once()) == script_runner.SCRIPT_RUN_CAP == 2
    assert reads == [(2, ["enabled", "id", "rule_id", "script_id", "thread_id"])]  # no run's input
    assert sorted(_outcomes(rule["id"])[0][0] for rule in rules) == ["queued"] * 4 + ["ran"] * 2


def test_a_scheduled_run_in_flight_keeps_a_rule_run_queued_and_the_other_way_round(runs):
    from automation import script_rule_runs

    script_id = _script(runs, "shared.sh", cadence=3600)
    rule = _rule(script_id)
    due = (_START + timedelta(minutes=1)).isoformat()
    db = get_db()
    try:
        [scheduled] = script_runner.claim_due(due, 2, set(), db)
        _emit()
        assert script_rule_runs.claim(due, 2, {script_id}, db) == []
        assert _outcomes(rule["id"]) == [("queued", None)]
        script_runner.settle(scheduled, script_runner._error_report(scheduled, "done"), due, db)
        [(_, rule_run)] = script_rule_runs.claim(due, 2, set(), db)
        later = (_START + timedelta(hours=2)).isoformat()  # the next slot is due, the script is busy
        assert script_runner.claim_due(later, 2, {rule_run.key, script_id}, db) == []
        script_runner.settle(scheduled, script_runner._error_report(scheduled, "done"), later, db,
                             rule_run=rule_run)
        assert [s.id for s in script_runner.claim_due(later, 2, set(), db)] == [script_id]
    finally:
        db.close()
    assert runs["calls"] == []  # claims only; nothing was spawned


def test_a_rules_only_script_stores_no_next_run_and_only_a_script_may(runs):
    import sqlite3

    rules_only = _script(runs, "stored.sh")
    db = get_db()
    try:
        assert db.execute("SELECT next_run_at FROM automation_schedules WHERE id=?",
                          (rules_only,)).fetchone()[0] is None
        assert script_schedules.due_scripts("9999-01-01T00:00:00+00:00", db=db) == []
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO automation_schedules (id,thread_id,scope_id,kind,spec,next_run_at,max_runs,"
                "created_by_actor,created_at,updated_at) VALUES ('c',?,'p','cadence','60',NULL,1,?,'t','t')",
                (OWNER, OWNER))
    finally:
        db.close()


@pytest.mark.parametrize("refusal", ["turned off", "cancelled", "paused", "deleted", "owner gone",
                                     "owner paused"])
def test_a_run_refused_before_it_starts_is_skipped_and_explain_says_it_did_not_run(
        runs, monkeypatch, refusal):
    from automation import script_rule_runs
    from events import rule_runs

    script_id = _script(runs, "refused.sh")
    rule = _rule(script_id)
    event_id = _emit()
    reason = {
        "turned off": rule_runs.TURNED_OFF, "cancelled": "its script schedule is cancelled",
        "paused": "its script schedule is paused", "deleted": "its script schedule no longer exists",
        "owner gone": "its owner is no longer available", "owner paused": rule_runs.OWNER_PAUSED,
    }[refusal]
    if refusal == "turned off":
        _tool(OWNER, operation="disable", rule_id=rule["id"])
    elif refusal in ("cancelled", "paused"):
        _write("UPDATE automation_schedules SET state=? WHERE id=?", refusal, script_id)
    elif refusal == "deleted":
        _write("DELETE FROM automation_schedules WHERE id=?", script_id)
    elif refusal == "owner gone":
        monkeypatch.setattr(script_rule_runs, "thread_is_available", lambda _row: False)
    else:
        import plans.public

        monkeypatch.setattr(plans.public, "thread_paused_since", lambda _db, _thread: "2026-01-01")
    _sweep()
    assert runs["calls"] == [] and _outcomes(rule["id"]) == [("skipped", reason)]
    assert _explained(event_id) == [("skipped", f"rule {rule['id']} did not run {OWNER}'s script: {reason}")]
