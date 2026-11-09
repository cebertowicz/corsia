"""The Workflows read: coverage and every per-run field, each checked against the module that owns it.

The clock is pinned (``NOW``), so the window is fixed: a Monday with the four completed weeks before it,
2026-09-21 to 2026-10-18. Runs, steps and the run's own record are seeded as the plan tables hold them, and
each run's record is written by ``plans.run_end`` itself rather than copied in.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from api.measurement_workflows import workflow_runs
from core.measurement_scope import Grain, MeasurementScope, resolve_grain
from model_identity_fixtures import seed_identity_catalog, seed_manifest
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_workflow_verdict_read import correction

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
WINDOW = {"from": "2026-09-21", "to": "2026-10-18"}
V3 = "20261001aa:0e2f1c9d3b4a5f60718293a4b5c6d7e8f9012345"
V4 = "20261010bb:9ab4f7e6d5c4b3a2918273645f4e3d2c1b0a9876"
CLOSE = "month-end-close"
INVOICE = "invoice-follow-up"
OPUS = "claude-opus-5-5"


@pytest.fixture(autouse=True)
def measured_owners(monkeypatch):
    """Resolve a workflow's measure owner through the shipped live rule, stubbed as its own test stubs it.

    ``tools.workflow_owners`` reads the owner thread's own skill folder, which a unit fixture has none of.
    """
    from core import workflow_verdict_read

    def owners(thread_id, _db):
        if thread_id != "root":
            return {}
        return {CLOSE: SimpleNamespace(skill_dir=Path(f"/workflows/{CLOSE}"), owner=thread_id, named=None)}

    monkeypatch.setattr(workflow_verdict_read, "thread_workflow_owners", owners)


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    connection.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('alpha','Alpha','t','t')")
    thread(connection, "root", title="Books agent")
    thread(connection, "billing", title="Billing agent")
    plan(connection, "plan-root", "root")
    plan(connection, "plan-billing", "billing")
    seed_identity_catalog(connection)
    seed_manifest(connection, "m", "claude-code", OPUS, "catalog")
    try:
        yield connection
    finally:
        connection.close()


def thread(db, thread_id: str, *, title: str) -> None:
    db.execute("INSERT INTO threads (id,user_id,title,project_id,agent_enabled,agent_status,created_at,updated_at) "
               "VALUES (?,'steve',?,'alpha',1,'active','t','t')", (thread_id, title))


def plan(db, plan_id: str, thread_id: str) -> None:
    db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) "
               "VALUES (?,'thread',?,'active',1,'t','t')", (plan_id, thread_id))


def phase(db, node_id: str, plan_id: str, *, workflow: str = CLOSE, version: str | None = V3,
          created: str, state: str = "accepted", ended: str = "2026-10-19T00:00:00Z") -> None:
    """A run: the phase that carries the workflow, as ``plans.workflow_fields`` writes it."""
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,"
                   "size,workflow_ref,workflow_version,commitment_id,requirements_revision,revision,created_at,"
                   "updated_at) VALUES (?,?,NULL,1,'phase',?,'Run recorded',?,'M',?,?,?,1,1,?,?)",
                   (node_id, plan_id, f"Run {workflow}", state, workflow, version, node_id, created, ended))


def step(db, node_id: str, plan_id: str, parent: str, step_id: str, *, title: str, workflow: str = CLOSE,
         version: str | None = V3, required: bool = True, gate: str | None = None, mode: str | None = None,
         state: str = "accepted", created: str, ended: str, order: int = 0, follows: str | None = None,
         closed_by_run: bool = False) -> None:
    """One step of a run, with the creation flags ``plans.run_end`` reads its record from."""
    created_payload = {"workflow_required": required, "workflow_by": "agent"}
    if gate is not None:
        created_payload.update({"workflow_gate": gate, "workflow_gate_mode": mode or "person"})
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,follow_up_of,order_key,kind,title,"
                   "acceptance_criteria,state,difficulty,size,workflow_ref,workflow_version,workflow_step,"
                   "commitment_id,requirements_revision,revision,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,'task',?,'Check',?,'routine','M',?,?,?,?,1,1,?,?)",
                   (node_id, plan_id, parent, follows, order, title, state, workflow, version, step_id, node_id,
                    created, ended))
        db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,payload_json,created_at) "
                   "VALUES (?,?,'plans.workflow_run','node_created',?,?)",
                   (f"created-{node_id}", node_id, json.dumps(created_payload), created))
        if state == "closed" and closed_by_run:
            db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                       "VALUES (?,?,'plans.workflow_run','state_transition','pending','closed',?)",
                       (f"end-{node_id}", node_id, ended))
        elif state != "pending":
            db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                       "VALUES (?,?,'test',?,'active',?,?)", (f"end-{node_id}", node_id, state, state, ended))


def usage(db, usage_id: str, node_id: str, at: str, *, model: str = "claude-code/opus",
          prompt: int = 1_000_000, source: str = "external_harness:claude-code",
          manifest: str | None = "m") -> None:
    """One priced call, inside the step's own working window (the chain reads a node to its end)."""
    db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,"
               "cache_read_tokens,total_tokens,source,created_at,plan_id,plan_node_id,plan_attempt_id,"
               "plan_commitment_id,plan_usage_role,context_manifest_id) VALUES (?,'root',?,?,0,0,0,?,"
               "?,?,?,?,'attempt',?,'execution',?)",
               (usage_id, model, prompt, prompt, source, at, "plan-root", node_id, node_id, manifest))


def turn(db, turn_id: str, node_id: str, *, started: str, ended: str, thread_id: str = "root") -> None:
    """One turn of working time on a node, which is what the cost rollup counts as working."""
    db.execute("INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch,"
               "plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) "
               "VALUES (?,?,0,?,'completed',?,?,'boot','plan-root',?,'attempt',?,'execution')",
               (turn_id, thread_id, OPUS, started, ended, node_id, node_id))


def task(db, node_id: str, plan_id: str, *, created: str, title: str = "A task outside any workflow") -> None:
    """An accepted task that ran under no workflow, which coverage counts in the denominator only."""
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,"
                   "commitment_id,requirements_revision,revision,created_at,updated_at) "
                   "VALUES (?,?,NULL,1,'task',?,'done','accepted',?,1,1,?,?)",
                   (node_id, plan_id, title, node_id, created, created))
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                   "VALUES (?,?,'test','accepted','active','accepted',?)", (f"end-{node_id}", node_id, created))


def end_run(db, node_id: str, state: str) -> None:
    """Let ``plans.run_end`` write the run's own record once its last step settled."""
    from plans import run_end

    with raw_plan_seed(db):
        run_end.record_if_ended(db, node_id, state)
        db.commit()


def gate_send_back(db, node_id: str, *, verdict: str, at: str) -> None:
    """The person's verdict at an acceptance gate, and the sent-back fact a rejection writes."""
    from core.sent_back_corrections import record_rejection_correction

    event = f"rejected-{node_id}"
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,payload_json,created_at) "
                   "VALUES (?,?,'test','review_rejected',?,?)",
                   (event, node_id, json.dumps({"detail": {"verdict": verdict}}), at))
        if verdict != "approved":
            record_rejection_correction(db, event)
        db.commit()


def measure(db, owner: str, workflow: str, *, name: str = "balanced") -> None:
    """One owner-declared KPI, which is what records who owns a workflow."""
    db.execute("INSERT INTO measure_definitions (owner_thread_id,kind,name,version,spec_json,file_path,"
               "file_sha256,synced_at) VALUES (?,'kpi',?,1,?,?, 'sha','2026-10-01T00:00:00Z')",
               (owner, name, json.dumps({"better": "lower", "review": 0.2, "target": 0.1, "can_decide": False}),
                f"/workflows/{workflow}/measures.yaml"))


def seed() -> None:
    """Two workflow versions of one workflow and a second workflow, with one run per case."""
    from infra.database import get_db

    db = get_db()
    try:
        measure(db, "root", CLOSE)
        # A clean version 3 run: both required steps done, one gate round, priced, 20 minutes working.
        phase(db, "close-v3-a", "plan-root", version=V3, created="2026-10-07T09:00:00Z")
        step(db, "a-gather", "plan-root", "close-v3-a", "gather", title="Gather statements", order=0,
             created="2026-10-07T09:10:00Z", ended="2026-10-07T09:30:00Z")
        step(db, "a-reconcile", "plan-root", "close-v3-a", "reconcile", title="Reconcile accounts", order=1,
             gate="acceptance", created="2026-10-07T09:40:00Z", ended="2026-10-07T10:00:00Z")
        usage(db, "u-a-gather", "a-gather", "2026-10-07T09:20:00Z")
        turn(db, "t-a-gather", "a-gather", started="2026-10-07T09:10:00Z", ended="2026-10-07T09:30:00Z")
        gate_send_back(db, "a-reconcile", verdict="approved", at="2026-10-07T10:00:00Z")
        end_run(db, "a-reconcile", "accepted")

        # A version 4 run whose required step the agent failed: the rest never ran.
        phase(db, "close-v4-b", "plan-root", version=V4, created="2026-10-14T09:00:00Z", state="closed")
        step(db, "b-gather", "plan-root", "close-v4-b", "gather", title="Gather statements", order=0,
             state="failed", created="2026-10-14T09:10:00Z", ended="2026-10-14T09:40:00Z")
        step(db, "b-reconcile", "plan-root", "close-v4-b", "reconcile", title="Reconcile accounts", order=1,
             gate="acceptance", state="closed", closed_by_run=True,
             created="2026-10-14T09:10:00Z", ended="2026-10-14T09:40:00Z")
        usage(db, "u-b-gather", "b-gather", "2026-10-14T09:20:00Z")
        turn(db, "t-b-gather", "b-gather", started="2026-10-14T09:10:00Z", ended="2026-10-14T09:25:00Z")
        end_run(db, "b-gather", "failed")

        # A version 4 run the person sent back at the gate, then it settled: two gate rounds.
        phase(db, "close-v4-c", "plan-root", version=V4, created="2026-10-15T09:00:00Z")
        step(db, "c-gather", "plan-root", "close-v4-c", "gather", title="Gather statements", order=0,
             created="2026-10-15T09:10:00Z", ended="2026-10-15T09:20:00Z")
        step(db, "c-reconcile", "plan-root", "close-v4-c", "reconcile", title="Reconcile accounts", order=1,
             gate="acceptance", state="accepted_with_followup", created="2026-10-15T09:30:00Z",
             ended="2026-10-15T10:00:00Z")
        step(db, "c-reconcile-2", "plan-root", "close-v4-c", "reconcile", title="Reconcile accounts", order=2,
             gate="acceptance", follows="c-reconcile", created="2026-10-15T10:30:00Z",
             ended="2026-10-15T11:00:00Z")
        usage(db, "u-c-gather", "c-gather", "2026-10-15T09:15:00Z")
        turn(db, "t-c-gather", "c-gather", started="2026-10-15T09:10:00Z", ended="2026-10-15T09:20:00Z")
        gate_send_back(db, "c-reconcile", verdict="changes_requested", at="2026-10-15T10:15:00Z")
        end_run(db, "c-reconcile-2", "accepted")

        # A version 3 run that settled clean and was corrected by the person two days later.
        phase(db, "close-v3-d", "plan-root", version=V3, created="2026-10-16T09:00:00Z")
        step(db, "d-report", "plan-root", "close-v3-d", "report", title="Draft close report", order=0,
             required=False, created="2026-10-16T09:10:00Z", ended="2026-10-16T09:30:00Z")
        turn(db, "t-d-report", "d-report", started="2026-10-16T09:10:00Z", ended="2026-10-16T09:40:00Z")
        end_run(db, "d-report", "accepted")
        correction(db, "msg-d", "d-report", at="2026-10-18T12:00:00Z")

        # A run of the same workflow before the window opened, and an unversioned run inside it.
        phase(db, "close-v3-old", "plan-root", version=V3, created="2026-08-31T09:00:00Z",
              ended="2026-09-01T09:00:00Z")
        step(db, "old-gather", "plan-root", "close-v3-old", "gather", title="Gather statements", order=0,
             created="2026-08-31T09:10:00Z", ended="2026-09-01T09:00:00Z")
        end_run(db, "old-gather", "accepted")
        phase(db, "close-unversioned", "plan-root", version=None, created="2026-10-13T09:00:00Z")
        step(db, "un-gather", "plan-root", "close-unversioned", "gather", title="Gather statements",
             version=None, created="2026-10-13T09:10:00Z", ended="2026-10-13T09:20:00Z")
        end_run(db, "un-gather", "accepted")

        # A second workflow, run once by another agent, with an unpriced call.
        phase(db, "invoice-1", "plan-billing", workflow=INVOICE, version=V3, created="2026-10-12T09:00:00Z")
        step(db, "i-send", "plan-billing", "invoice-1", "send", title="Send the reminder",
             workflow=INVOICE, created="2026-10-12T09:10:00Z", ended="2026-10-12T09:20:00Z")
        usage(db, "u-i-send", "i-send", "2026-10-12T09:15:00Z", model="codex/gpt-5.6-terra",
              source="external_harness:codex", manifest=None)
        end_run(db, "i-send", "accepted")

        # Two tasks that ran under no workflow at all.
        task(db, "plain-1", "plan-root", created="2026-10-08T09:00:00Z")
        task(db, "plain-2", "plan-root", created="2026-10-09T09:00:00Z")
        db.commit()
    finally:
        db.close()


def read(scope: MeasurementScope | None = None) -> dict[str, Any]:
    """The section for the pinned grain and scope."""
    from infra.database import get_db

    grain: Grain = resolve_grain("weeks", "UTC", now=NOW)
    connection = get_db()
    try:
        return workflow_runs(connection, "steve", scope or MeasurementScope("all", None), grain, now=NOW)
    finally:
        connection.close()


@pytest.fixture
def section(db):
    seed()
    return read()


def rows_by_id(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["run_id"]: row for workflow in body["workflows"] for row in workflow["runs"]}


def entry_by_ref(body: dict[str, Any], ref: str) -> dict[str, Any]:
    return next(entry for entry in body["workflows"] if entry["ref"] == ref)


def test_the_runs_the_engine_never_compared_are_counted_with_its_reason(section) -> None:
    # The unversioned run of this workflow is excluded by read_snapshot; the count is its dict, not a re-count.
    assert entry_by_ref(section, CLOSE)["excluded"] == {"unversioned": 1}
    assert entry_by_ref(section, INVOICE)["excluded"] == {}


def test_the_verdict_is_what_the_engine_decides_for_the_current_version(section, db) -> None:
    """The section's verdict equals ``decide`` on the same full history, at the same pinned clock."""
    from core.workflow_verdict_read import read_snapshot
    from core.workflow_verdict_rules import decide, freeze_plan
    from core.workflow_verdict_types import subject_of

    snapshot = read_snapshot(db, now=NOW, workflow_refs=[CLOSE])
    history = next(item for item in snapshot.workflows if item.ref == CLOSE)
    current = history.versions()[-1]  # version by first use, so the later one is current
    assert current == V4 and history.versions() == [V3, V4]
    subject = subject_of("workflow", CLOSE, current)
    expected = decide(history, snapshot.plans.get(subject) or freeze_plan(history, current), NOW,
                      history.successor(current))
    verdict = entry_by_ref(section, CLOSE)["verdict"]
    assert verdict == {"word": expected.word, "reason": expected.reason, "look": expected.look,
                       "inputs": dict(expected.inputs)}
    # The engine's own words for a workflow with no recorded plan or baseline, never a null standing in for one.
    assert (verdict["word"], verdict["reason"], verdict["look"]) == ("not_enough_data", "no_baseline", 0)


def test_an_unrelated_reached_record_never_reaches_the_section(section, db) -> None:
    """The section reads only the current version's record: an older version's reached verdict stays out."""
    from core.workflow_verdict_evaluator import reached_detail, write_verdicts
    from core.workflow_verdict_read import read_snapshot
    from core.workflow_verdict_rules import Evaluation, Verdict, freeze_plan
    from core.workflow_verdict_types import subject_of

    assert reached_detail(db, []) == {}  # no subjects of interest reads no records at all
    history = next(item for item in read_snapshot(db, now=NOW, workflow_refs=[CLOSE]).workflows
                   if item.ref == CLOSE)
    older = history.versions()[0]  # V3: superseded by the current version, so not this entry's verdict
    write_verdicts(db, [Evaluation(subject_of("workflow", CLOSE, older), "root", freeze_plan(history, older),
                                   Verdict("revert", "quality_fell", 3, (), {}), False)])
    assert reached_detail(db, [subject_of("workflow", CLOSE, older)])[
        subject_of("workflow", CLOSE, older)]["word"] == "revert"
    verdict = entry_by_ref(read(), CLOSE)["verdict"]
    assert "revert" not in json.dumps(verdict) and verdict["word"] == "not_enough_data"


def test_a_reached_verdict_is_read_from_its_record_and_never_redecided(section, db) -> None:
    """A stored keep/revert is the verdict: ``compute`` skips it, so the record is the only source."""
    from core.workflow_verdict_evaluator import write_verdicts
    from core.workflow_verdict_read import read_snapshot
    from core.workflow_verdict_rules import Evaluation, Verdict, freeze_plan
    from core.workflow_verdict_types import subject_of

    history = next(item for item in read_snapshot(db, now=NOW, workflow_refs=[CLOSE]).workflows
                   if item.ref == CLOSE)
    current = history.versions()[-1]
    subject = subject_of("workflow", CLOSE, current)
    write_verdicts(db, [Evaluation(subject, "root", freeze_plan(history, current),
                                   Verdict("keep", "quality_held", 2, (), {"baseline": V3}), False)])
    assert subject in read_snapshot(db, now=NOW, workflow_refs=[CLOSE]).reached
    assert entry_by_ref(read(), CLOSE)["verdict"] == {
        "word": "keep", "reason": "quality_held", "look": 2, "inputs": {"baseline": V3}}


def test_the_window_is_the_pages_current_window_and_coverage_counts_the_pages_tasks(section) -> None:
    assert section["window"] == WINDOW
    # The page's task set: every task accepted inside the window, whether or not a workflow ran it.
    # Eight of the ten sit under a run, including the failed run's accepted step and the unversioned run's.
    assert section["coverage"] == {"under_workflow": 8, "all_tasks": 10}


def test_each_workflow_is_one_entry_biggest_first_with_its_owner(section) -> None:
    assert [entry["ref"] for entry in section["workflows"]] == [CLOSE, INVOICE]
    close, invoice = section["workflows"]
    assert close["owner"] == {"id": "root", "label": "Books agent"}
    # A workflow nobody has declared measures for has no owner to name, which is not the runner.
    assert invoice["owner"] is None
    # Nothing on dev records a workflow's human name; the reference is the identity.
    assert close["name"] is None and invoice["name"] is None
    assert [row["run_id"] for row in close["runs"]] == ["close-v3-a", "close-v4-b", "close-v4-c", "close-v3-d"]


def test_a_clean_run_carries_its_date_version_agent_cost_minutes_and_one_gate_round(section) -> None:
    from tools.workflow_task_fields import version_short_hash

    row = rows_by_id(section)["close-v3-a"]
    assert row["started"] == "2026-10-07"
    assert row["version"] == version_short_hash(V3)
    assert row["agent"] == {"id": "root", "label": "Books agent"}
    assert row["minutes"] == 20.0
    assert (row["failed_step"], row["caught"], row["gate_rounds"], row["owner_measure"]) == (None, None, 1, None)
    assert row["cost"] is not None and row["cost"] > 0.0


def test_a_run_the_agent_failed_names_the_step_and_says_the_agent_caught_it(section) -> None:
    rows = rows_by_id(section)
    assert (rows["close-v4-b"]["failed_step"], rows["close-v4-b"]["caught"]) == ("Gather statements", "agent_closed")
    assert rows["close-v4-b"]["gate_rounds"] == 1  # the gate step was built, then cut short: still one round
    assert rows["close-v4-b"]["minutes"] == 15.0


def test_a_send_back_at_the_gate_counts_two_rounds_and_is_how_the_run_was_caught(section) -> None:
    rows = rows_by_id(section)
    assert (rows["close-v4-c"]["caught"], rows["close-v4-c"]["gate_rounds"]) == ("sent_back", 2)
    assert rows["close-v4-c"]["failed_step"] is None


def test_a_person_correction_after_the_run_settled_reads_corrected_later(section) -> None:
    rows = rows_by_id(section)
    assert rows["close-v3-d"]["caught"] == "corrected_later"
    # A workflow with no acceptance-gate step has no rounds to report, which is not zero rounds.
    assert rows["close-v3-d"]["gate_rounds"] is None
    assert rows["close-v3-d"]["minutes"] == 30.0


def test_cost_is_the_pages_own_price_or_null_and_never_a_default(section) -> None:
    from api.measurement_model_cost import query_model_costs
    from core.cost_identity import IdentityResolver
    from core.workflow_verdict_read import history_grain
    from infra.database import get_db

    params = {"user_id": "steve", "scope_project": "all", "scope_agent": None,
              "grain": history_grain(datetime(2026, 8, 31).date(), NOW)}
    connection = get_db()
    try:
        figures = {row["task_id"]: row["dollars"] for row in query_model_costs(connection, params, IdentityResolver(connection))}
    finally:
        connection.close()
    rows = rows_by_id(section)
    assert rows["close-v3-a"]["cost"] == pytest.approx(figures["a-gather"])
    assert rows["close-v4-c"]["cost"] == pytest.approx(figures["c-gather"])
    # A call with no priced listing stays unknown; a run with no call of its own reads 0.0, not null.
    assert rows["invoice-1"]["cost"] is None
    assert rows["close-v3-d"]["cost"] == 0.0


def test_the_unversioned_run_and_the_run_outside_the_window_are_not_listed(section) -> None:
    listed = set(rows_by_id(section))
    assert "close-v3-old" not in listed and "close-unversioned" not in listed


def test_a_selected_agent_sees_only_its_own_runs(db) -> None:
    seed()
    only_billing = read(MeasurementScope("alpha", "billing"))
    assert [entry["ref"] for entry in only_billing["workflows"]] == [INVOICE]
    assert "root" not in json.dumps(only_billing)
    # The other agent's plan is out of this scope: its two untagged tasks leave the denominator too.
    assert read(MeasurementScope("alpha", "root"))["coverage"] == {"under_workflow": 7, "all_tasks": 9}


def test_the_route_answers_for_the_page_parameters_and_the_live_window(db) -> None:
    """The whole route, on dates fixed against the live clock so no date can make this read differently."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import measurement

    live = resolve_grain("weeks", "UTC")
    inside = (date.fromisoformat(live.latest.first) + timedelta(days=1)).isoformat()
    in_progress = (date.fromisoformat(live.current) + timedelta(days=1)).isoformat()
    for node_id, started in (("clock-run-in", inside), ("clock-run-late", in_progress)):
        phase(db, node_id, "plan-root", created=f"{started}T09:00:00Z", ended=f"{started}T10:00:00Z")
        step(db, f"{node_id}-step", "plan-root", node_id, "gather", title="Gather statements",
             created=f"{started}T09:10:00Z", ended=f"{started}T10:00:00Z")
        end_run(db, f"{node_id}-step", "accepted")
    task(db, "clock-plain", "plan-root", created=f"{inside}T09:00:00Z")
    db.commit()

    app = FastAPI()
    app.include_router(measurement.router)
    app.dependency_overrides[measurement.require_user] = lambda: {"id": "steve"}
    response = TestClient(app).get("/api/measurement/workflows?project=all&agent=none&grain=weeks&timezone=UTC")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"definition_version", "scope", "period", "window", "coverage", "workflows"}
    assert body["definition_version"] == 1 and body["scope"] == {"project": "all", "agent": None}
    assert body["window"] == {"from": live.latest.first, "to": live.period_end(live.latest.last)}
    assert body["coverage"] == {"under_workflow": 1, "all_tasks": 2}
    (entry,) = body["workflows"]
    assert [run["run_id"] for run in entry["runs"]] == ["clock-run-in"]
    assert entry["ref"] == CLOSE and entry["owner"] is None
