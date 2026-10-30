"""The verdict's read side from real schema rows: runs, groups, settlement, corrections, definitions and cost."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.cost_identity import IdentityResolver
from api.measurement_model_cost import query_model_costs
from core.workflow_verdict_read import (
    _OPEN_WORKFLOWS_SQL, open_workflow_subjects, price_snapshot, read_open_snapshot, read_snapshot,
)
from core.workflow_verdict_rules import compute, freeze_plan
from core.workflow_verdicts import MIXED, KpiRow
from infra.guidance_history_paths import LOCAL_EDIT_MARK
from model_identity_fixtures import seed_identity_catalog, seed_manifest
from tests.plan_writer_authorizer import raw_plan_seed
from tests.verdict_calibration_helpers import calibration_script

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)
OPUS = "claude-opus-5-5"


@pytest.fixture(autouse=True)
def live_workflow_owners(monkeypatch):
    """Verdicts associate definitions through the shipped live owner rule, never stored metadata."""
    from core import workflow_verdict_read

    owned = {"owner": "sdlc-process", "brief-owner": "decision-brief"}

    def owners(thread_id, _db):
        if thread_id not in owned:
            return {}
        workflow = owned[thread_id]
        return {workflow: SimpleNamespace(skill_dir=Path(f"/workflows/{workflow}"), owner=thread_id, named=None)}

    monkeypatch.setattr(workflow_verdict_read, "thread_workflow_owners", owners)


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    connection.execute("INSERT INTO threads (id,user_id,title,agent_enabled,agent_status,created_at,updated_at) "
                       "VALUES ('root','steve','root',1,'active','t','t')")
    connection.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) "
                       "VALUES ('plan','thread','root','active',1,'t','t')")
    seed_identity_catalog(connection)
    seed_manifest(connection, "m", "claude-code", OPUS, "catalog")
    try:
        yield connection
    finally:
        connection.close()


def node(db, node_id: str, *, workflow: str | None = "sdlc-process", version: str | None = "v1",
         state: str = "accepted", parent: str | None = None, difficulty: str | None = "standard",
         size: str | None = "M", created: str = "2026-10-01T00:00:00Z", ended: str = "2026-10-05T00:00:00Z",
         kind: str = "task", revises: str | None = None) -> None:
    """A node; a ``revises`` node is the sent-back task's revision, created beside it as the rejection does."""
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,follow_up_of,order_key,kind,title,acceptance_criteria,"
                   "state,commitment_id,workflow_ref,workflow_version,difficulty,size,requirements_revision,revision,"
                   "created_at,updated_at) VALUES (?,'plan',?,?,1,?,'task','done',?,?,?,?,?,?,1,1,?,?)",
                   (node_id, parent, revises, kind, state, node_id, workflow, version, difficulty, size, created, ended))
        if revises:
            db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,created_at) "
                       "VALUES (?,?,'test','revision_created',?)", (f"revise-{node_id}", node_id, created))
        if state in {"accepted", "accepted_with_followup", "failed", "closed"}:
            db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                       "VALUES (?,?,'test',?,'active',?,?)", (f"end-{node_id}", node_id, state, state, ended))


def usage(db, usage_id: str, node_id: str, model: str = "claude-code/opus", *, prompt: int = 1_000_000,
          manifest: str | None = "m") -> None:
    db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,"
               "cache_read_tokens,total_tokens,source,created_at,plan_id,plan_node_id,plan_attempt_id,"
               "plan_commitment_id,plan_usage_role,context_manifest_id) VALUES (?,'root',?,?,0,0,0,?,"
               "'external_harness:claude-code','2026-10-02T00:00:00Z','plan',?,'attempt',?,'execution',?)",
               (usage_id, model, prompt, prompt, node_id, node_id, manifest))


def correction(db, message_id: str, node_id: str, *, at: str = "2026-10-06T00:00:00Z", label: str = "correction",
               counter: str = "c1") -> None:
    """One owner message, answered after a turn working ``node_id``, with a writer-recorded fact."""
    from core.corrections_recording import ACTIVE_VERSION_SETTING, ClassifierVersion, CorrectionCandidate, SOURCE_KIND_MESSAGE, version_key
    from infra.settings import set_setting
    from jobs.corrections_recording import _record_fact

    db.execute("INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,plan_id,"
               "plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) "
               "VALUES (?,'root',0,'m','completed',?,'boot','plan',?,'attempt',?,'execution')",
               (f"turn-{message_id}", at[:9] + str(int(at[9]) - 1) + at[10:], node_id, node_id))
    db.execute("INSERT INTO messages (id,thread_id,role,content,created_at) VALUES (?,'root','user','x',?)",
               (message_id, at))
    db.commit()
    candidate = CorrectionCandidate(SOURCE_KIND_MESSAGE, message_id, "root", None, "x", at)
    payload = {"label": label, "truncated": False, "original_tokens": 1, "failure_reason": "none"}
    version = ClassifierVersion("asset", counter, "head", 0.5, "policy")
    set_setting(ACTIVE_VERSION_SETTING, {"key": version_key(version)}, db=db)
    assert _record_fact(candidate, payload, version) is True


def runs_of(snapshot, workflow: str = "sdlc-process"):
    (history,) = [item for item in snapshot.workflows if item.ref == workflow]
    return {run.run_id: run for run in history.runs}, history


def test_a_run_is_the_outermost_node_carrying_its_workflow_and_only_judged_versions_count(db) -> None:
    node(db, "run")
    node(db, "same", parent="run")
    node(db, "brief", parent="run", workflow="decision-brief", version="b1")
    node(db, "unversioned", version=None)
    node(db, "local", version=f"abc{LOCAL_EDIT_MARK}")
    snapshot = read_snapshot(db, now=NOW)
    runs, history = runs_of(snapshot)
    assert set(runs) == {"run"} and dict(history.excluded) == {"unversioned": 1, "local_edit": 1}
    assert set(runs_of(snapshot, "decision-brief")[0]) == {"brief"}


def test_settled_means_terminal_and_past_the_window_and_failed_runs_are_not_outcomes(db) -> None:
    node(db, "done")
    node(db, "failed", state="failed", ended="2026-10-06T00:00:00Z")
    node(db, "recent", ended="2026-10-18T00:00:00Z")
    node(db, "open", state="active")
    runs, _ = runs_of(read_snapshot(db, now=NOW))
    assert (runs["failed"].terminal_at, runs["failed"].outcome) == ("2026-10-06 00:00:00", False)
    assert runs["done"].outcome and runs["done"].settled(3, NOW) and runs["failed"].settled(3, NOW)
    assert not runs["recent"].settled(3, NOW) and runs["recent"].settled(0, NOW)
    assert runs["open"].terminal_at is None and not runs["open"].settled(0, NOW)


def test_the_group_is_the_runs_own_model_difficulty_and_size_band(db) -> None:
    node(db, "opus", difficulty="hard", size="XL")
    usage(db, "u1", "opus", prompt=900)
    usage(db, "u2", "opus", "codex/gpt-5.6-terra", prompt=100, manifest=None)
    node(db, "worker", parent="opus", workflow=None)
    usage(db, "u3", "worker", "codex/gpt-5.6-terra", prompt=100_000, manifest=None)  # a worker's tokens are not own
    node(db, "split", size="S")
    usage(db, "u4", "split", prompt=500)
    usage(db, "u5", "split", "codex/gpt-5.6-terra", prompt=500, manifest=None)
    node(db, "silent", difficulty=None, size=None)
    runs, _ = runs_of(read_snapshot(db, now=NOW))
    assert runs["opus"].group == (OPUS, "hard", "L+")
    assert runs["split"].group == (MIXED, "standard", "XS-S")
    assert runs["silent"].group == (MIXED, "unrated", "unsized")


def test_corrections_reach_their_run_through_the_correction_attribution(db) -> None:
    node(db, "run")
    node(db, "step", parent="run", workflow=None)
    node(db, "loose", workflow=None)
    correction(db, "m1", "step")
    correction(db, "m2", "step", label="not_correction", at="2026-10-07T00:00:00Z")
    correction(db, "m3", "loose", at="2026-10-08T00:00:00Z")
    runs, _ = runs_of(read_snapshot(db, now=NOW))
    facts = runs["run"].corrections
    assert [(fact.source_kind, fact.source_id, fact.is_correction, fact.counter) for fact in facts] == [
        ("message", "m1", True, "c1"), ("message", "m2", False, "c1")]


def _definitions(db, *, can_decide: bool = True, workflow: str = "sdlc-process", owner: str = "owner") -> None:
    event = {"attributes": {"pr": "id"}, "dedupe": ["pr"]}
    kpi = {"num": {"event": "qg.escape"}, "den": {"event": "qg.escape"}, "better": "lower", "can_decide": can_decide,
           "coverage": {"of": "qg.escape", "by": "audit"}, "target": 0.1}
    rows = [("event", "qg.escape", 1, event, "2026-09-01T00:00:00Z"),
            ("kpi", "escapes", 1, {**kpi, "review": 0.2}, "2026-09-01T00:00:00Z"),
            ("kpi", "escapes", 2, {**kpi, "review": 0.5}, "2026-10-10T00:00:00Z")]
    for kind, name, version, spec, synced in rows:
        db.execute("INSERT INTO measure_definitions (owner_thread_id,kind,name,version,spec_json,file_path,"
                   "file_sha256,synced_at) VALUES (?,?,?,?,?,?,'sha',?)",
                   (owner, kind, name, version, json.dumps(spec), f"/workflows/{workflow}/measures.yaml", synced))


def test_the_frozen_plan_uses_the_definitions_in_force_at_the_versions_first_run(db) -> None:
    _definitions(db)
    node(db, "first", created="2026-10-01T00:00:00Z")
    node(db, "second", version="v2", created="2026-10-12T00:00:00Z", ended="2026-10-14T00:00:00Z")
    _, history = runs_of(read_snapshot(db, now=NOW))
    assert history.owner == "owner"
    (v1,) = [m for m in freeze_plan(history, "v1").measures if m.source == "kpi"]
    (v2,) = [m for m in freeze_plan(history, "v2").measures if m.source == "kpi"]
    assert (v1.definition_version, v1.margin_gap) == (1, pytest.approx(0.1))
    assert (v2.definition_version, v2.margin_gap) == (2, pytest.approx(0.4))


def test_reached_verdicts_remain_skipped_after_an_old_event(db) -> None:
    node(db, "run")
    subject = "workflow:sdlc-process@v1"
    old = (NOW - timedelta(days=400)).isoformat()
    db.execute("INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,"
               "attributes) VALUES ('reached','reached','measurement.verdict.reached','corsia','instrumented',?,?,?,"
               "'{}')", (old, old, subject))
    snapshot = read_snapshot(db, now=NOW)
    assert subject in snapshot.reached and compute(snapshot) == []


def _reached(db, subject: str, index: int = 0) -> None:
    db.execute("INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,"
               "attributes) VALUES (?,?,'measurement.verdict.reached','corsia','instrumented','2026-10-01',"
               "'2026-10-01',?,'{}')", (f"reached-{index}", f"reached-{index}", subject))


def test_open_snapshot_selects_judged_versions_in_use_without_reached_verdicts(db) -> None:
    node(db, "run")
    node(db, "other", workflow="decision-brief", version="v9", created="2026-10-03T00:00:00Z")
    node(db, "done", version="v0")
    node(db, "plain", workflow="decision-brief", version=None)
    node(db, "edited", workflow="decision-brief", version=f"v2{LOCAL_EDIT_MARK}")
    correction(db, "bounded", "run")
    _reached(db, "workflow:sdlc-process@v0")

    assert open_workflow_subjects(db) == {"workflow:sdlc-process@v1", "workflow:decision-brief@v9"}
    snapshot = read_open_snapshot(db, now=NOW, limit=1)
    assert snapshot.subjects == {"workflow:decision-brief@v9"}  # latest use first
    snapshot = read_open_snapshot(db, now=NOW)
    assert [history.ref for history in snapshot.workflows if history.runs] == ["decision-brief", "sdlc-process"]
    runs, _ = runs_of(snapshot)
    assert runs["run"].corrections[0].source_id == "bounded"
    details = [str(row["detail"]) for row in db.execute(
        f"EXPLAIN QUERY PLAN {_OPEN_WORKFLOWS_SQL}", {"reached": "measurement.verdict.reached"})]
    assert not any("SCAN outcome_events" in detail for detail in details)


def test_kpi_rows_come_only_from_an_injected_reader(db) -> None:
    node(db, "run")
    runs, _ = runs_of(read_snapshot(db, now=NOW))
    assert runs["run"].kpi_rows is None  # no formula engine yet: deciding KPIs read Not comparable
    row = KpiRow("num", "observed", 1.0, "2026-10-05T00:00:00Z")
    read = read_snapshot(db, now=NOW, kpi_reader=lambda _db, found, _defs: {r.run_id: {"escapes": (row,)} for r in found})
    assert runs_of(read)[0]["run"].kpi_rows == {"escapes": (row,)}


def test_cost_prices_every_terminal_run_once_and_is_unknown_never_zero_only_when_a_call_cannot_be_priced(db) -> None:
    node(db, "priced")
    usage(db, "u1", "priced")
    node(db, "nested")
    usage(db, "u2", "nested")
    node(db, "child", parent="nested", workflow=None, ended="2026-10-04T00:00:00Z")
    usage(db, "u3", "child")
    node(db, "half")
    usage(db, "u4", "half")
    node(db, "broke", parent="half", workflow=None, state="failed")
    usage(db, "u4b", "broke")
    node(db, "failed", state="failed")
    usage(db, "u5", "failed")
    usage(db, "u5-late", "failed")
    db.execute("UPDATE llm_usage SET created_at='2026-10-06T00:00:00Z' WHERE id='u5-late'")  # after it failed
    node(db, "gap", state="failed")
    usage(db, "u6", "gap")
    db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,created_at) "
               "VALUES ('gap-event','gap','test','usage_recording_failed','2026-10-02T00:00:00Z')")
    node(db, "odd", state="closed")
    usage(db, "u7", "odd", model="codex/gpt-5.6-terra")
    db.execute("UPDATE llm_usage SET source='external_harness:codex' WHERE id='u7'")  # no proven price
    node(db, "empty", state="closed")
    priced = price_snapshot(db, read_snapshot(db, now=NOW))
    runs, _ = runs_of(priced)
    from core.workflow_verdict_read import history_grain
    params = {"user_id": "steve", "scope_project": "all", "scope_agent": None,
              "grain": history_grain(datetime(2026, 10, 1).date(), NOW)}
    by_task = {row["task_id"]: row["dollars"] for row in query_model_costs(db, params, IdentityResolver(db))}
    one_call = by_task["priced"]
    assert runs["priced"].cost == pytest.approx(one_call) and one_call > 0
    assert runs["nested"].cost == pytest.approx(by_task["nested"] + by_task["child"])
    # The failed child's call sits in its accepted ancestor's chain: counted once, in the run that holds it.
    assert runs["half"].cost == pytest.approx(by_task["half"]) == pytest.approx(2 * one_call)
    assert runs["failed"].cost == pytest.approx(one_call)  # its own call; the one after it failed is not its cost
    assert runs["empty"].cost == 0.0
    assert runs["gap"].cost is None and runs["odd"].cost is None
    assert db.execute("SELECT COUNT(*) FROM temp.measurement_task_chain").fetchone()[0] == 0


def run_costs(db) -> tuple[dict, dict]:
    """Each run's priced cost, and Measurement's own figure for every accepted task (no seeds)."""
    from core.workflow_verdict_read import history_grain

    priced = price_snapshot(db, read_snapshot(db, now=NOW))
    runs = {run.run_id: run for history in priced.workflows for run in history.runs}
    params = {"user_id": "steve", "scope_project": "all", "scope_agent": None,
              "grain": history_grain(datetime(2026, 10, 1).date(), NOW)}
    figures = {row["task_id"]: row["dollars"] for row in query_model_costs(db, params, IdentityResolver(db))}
    return {run_id: run.cost for run_id, run in runs.items()}, figures


def step(db, node_id: str, run: str, millions: int, *, state: str = "accepted", **fields) -> None:
    """A workflow step: a task under its run's phase, with one call of ``millions`` million prompt tokens."""
    node(db, node_id, workflow=None, parent=run, state=state, **fields)
    if millions:
        usage(db, f"call-{node_id}", node_id, prompt=millions * 1_000_000)


def test_an_accepted_phase_run_costs_the_sum_of_its_steps_measurement_figures(db) -> None:
    node(db, "run", kind="phase")
    step(db, "s1", "run", 1)
    step(db, "s2", "run", 2)
    costs, figures = run_costs(db)
    unit = figures["s1"]
    assert unit > 0 and figures["s2"] == pytest.approx(2 * unit)
    assert costs["run"] == pytest.approx(figures["s1"] + figures["s2"]) == pytest.approx(3 * unit)


def test_a_failed_phase_run_counts_every_call_in_its_steps_once(db) -> None:
    node(db, "run", kind="phase", state="failed")
    step(db, "ok", "run", 1)
    step(db, "bad", "run", 2, state="failed")
    step(db, "shut", "run", 4, state="closed")
    step(db, "sub", "shut", 8, state="active")
    costs, figures = run_costs(db)
    assert costs["run"] == pytest.approx(15 * figures["ok"])


def test_a_failed_task_under_an_accepted_task_is_counted_once_inside_it(db) -> None:
    node(db, "run", kind="phase")
    step(db, "unit", "run", 1)
    step(db, "top", "run", 1)
    step(db, "broke", "top", 2, state="failed")
    costs, figures = run_costs(db)
    assert figures["top"] == pytest.approx(3 * figures["unit"]) and "broke" not in figures
    assert costs["run"] == pytest.approx(figures["unit"] + figures["top"])


@pytest.mark.parametrize(("revision_state", "units"), [("accepted", 4), ("failed", 4), ("active", None)])
def test_a_sent_back_step_is_counted_once_and_unknown_while_its_revision_is_open(db, revision_state, units) -> None:
    node(db, "run", kind="phase", state="failed")
    step(db, "unit", "run", 1)
    step(db, "sent", "run", 1, state="closed")
    step(db, "again", "run", 2, state=revision_state, revises="sent")
    costs, figures = run_costs(db)
    assert costs["run"] == (None if units is None else pytest.approx(units * figures["unit"]))


def test_a_nested_failed_phase_run_is_priced_in_the_nested_run_only(db) -> None:
    node(db, "outer", kind="phase")
    step(db, "o1", "outer", 1)
    node(db, "inner", kind="phase", workflow="decision-brief", parent="outer", state="failed")
    step(db, "i1", "inner", 2, state="failed")
    costs, figures = run_costs(db)
    assert costs["outer"] == pytest.approx(figures["o1"]) and costs["inner"] == pytest.approx(2 * figures["o1"])


def test_phase_runs_price_late_calls_gaps_unpriceable_calls_and_empty_runs(db) -> None:
    node(db, "late", kind="phase", state="failed")
    step(db, "late1", "late", 1, state="failed")
    usage(db, "after", "late1")
    db.execute("UPDATE llm_usage SET created_at='2026-10-06T00:00:00Z' WHERE id='after'")  # after it failed
    node(db, "gap", kind="phase", state="failed")
    step(db, "gap1", "gap", 1, state="failed")
    db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,created_at) "
               "VALUES ('gap-event','gap1','test','usage_recording_failed','2026-10-02T00:00:00Z')")
    node(db, "odd", kind="phase", state="closed")
    step(db, "odd1", "odd", 1, state="closed")
    db.execute("UPDATE llm_usage SET source='external_harness:codex', model='codex/gpt-5.6-terra' WHERE id='call-odd1'")
    node(db, "quiet", kind="phase", state="closed")
    step(db, "quiet1", "quiet", 0, state="closed")
    node(db, "bare", kind="phase", state="closed")
    node(db, "ref", kind="phase")
    step(db, "ref1", "ref", 1)
    costs, figures = run_costs(db)
    assert costs["late"] == pytest.approx(figures["ref1"])
    assert costs["gap"] is None and costs["odd"] is None and costs["bare"] is None
    assert costs["quiet"] == 0.0
    assert db.execute("SELECT COUNT(*) FROM temp.measurement_task_chain").fetchone()[0] == 0


def test_calibration_script_reads_a_fixture_read_only_and_prints_aggregates_only(db, isolated_db, capsys) -> None:
    script = calibration_script()
    _definitions(db)
    _definitions(db, workflow="decision-brief", owner="brief-owner")
    for index in range(4):
        node(db, f"b{index}", workflow="decision-brief", created=f"2026-10-0{index + 1}T00:00:00Z")
    for index in range(66):
        version, day = ("v1", 1 + index % 9) if index < 44 else ("v2", 10 + index % 5)
        node(db, f"r{index}", version=version, created=f"2026-10-{day:02d}T00:00:00Z",
             ended=f"2026-10-{day + 1:02d}T00:00:00Z")
        usage(db, f"u{index}", f"r{index}")
        if index in {0, 1, 2, 44, 45}:  # distinct days, so each message follows only its own run's turn
            correction(db, f"m{index}", f"r{index}", at=f"2026-10-{day + 2:02d}T00:00:00Z")
    db.commit()
    assert script.main([str(isolated_db)]) == 0
    out = capsys.readouterr().out
    assert "A/A splits" in out and "corrections_per_outcome" in out and "(whole verdict)" in out
    assert "Not calibrated (no split could measure it): " in out
    assert "n=5 p50=1.00" in out and "v1 -> v2" in out and "no_baseline" in out
    assert not any(f"r{index}" in out.split() or f"m{index}" in out for index in range(66))


def test_calibration_script_refuses_when_a_calibrated_workflow_lacks_owner_kpi_definitions(db, isolated_db) -> None:
    _definitions(db)
    node(db, "sdlc-run")
    node(db, "brief-run", workflow="decision-brief", version="v1")
    db.commit()
    with pytest.raises(SystemExit, match="missing owner KPI definitions in force at first use for calibration in CORSIA_WORKSPACE: decision-brief"):
        calibration_script().main([str(isolated_db)])


def test_calibration_script_names_every_calibrated_workflow_with_no_runs(db, isolated_db) -> None:
    _definitions(db)
    node(db, "sdlc-run")
    db.commit()
    with pytest.raises(SystemExit, match="missing calibrated workflow history for calibration: decision-brief$"):
        calibration_script().main([str(isolated_db)])


def test_calibration_script_refuses_when_no_owner_kpi_can_decide(db, isolated_db) -> None:
    _definitions(db, can_decide=False)
    _definitions(db, workflow="decision-brief", owner="brief-owner")
    node(db, "sdlc-run")
    node(db, "brief-run", workflow="decision-brief")
    db.commit()
    with pytest.raises(SystemExit, match="missing owner KPI definitions in force at first use for calibration in CORSIA_WORKSPACE: sdlc-process"):
        calibration_script().main([str(isolated_db)])


def test_calibration_script_refuses_kpis_defined_only_after_every_first_use(db, isolated_db) -> None:
    # The KPI is in force now but was not when either sdlc-process version first ran, so no plan production
    # freezes would hold it; the refusal reads first use, never now.
    _definitions(db)
    _definitions(db, workflow="decision-brief", owner="brief-owner")
    node(db, "early-1", created="2026-08-01T00:00:00Z", ended="2026-08-02T00:00:00Z")
    node(db, "early-2", created="2026-08-03T00:00:00Z", ended="2026-08-04T00:00:00Z")
    node(db, "brief-run", workflow="decision-brief")
    db.commit()
    with pytest.raises(SystemExit, match="missing owner KPI definitions in force at first use for calibration in CORSIA_WORKSPACE: sdlc-process$"):
        calibration_script().main([str(isolated_db)])


def test_read_snapshot_never_writes_so_it_runs_on_the_query_only_lane(db) -> None:
    node(db, "run")
    usage(db, "u1", "run")
    correction(db, "m1", "run")
    db.commit()
    db.execute("PRAGMA query_only=ON")
    runs, _ = runs_of(read_snapshot(db, now=NOW))
    assert runs["run"].group[0] == OPUS
