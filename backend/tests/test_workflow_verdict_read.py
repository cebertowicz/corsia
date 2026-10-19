"""The verdict's read side from real schema rows: runs, groups, settlement, corrections, definitions and cost."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.cost_identity import IdentityResolver
from api.measurement_model_cost import query_model_costs
from core.workflow_verdict_read import price_snapshot, read_snapshot
from core.workflow_verdict_rules import freeze_plan
from core.workflow_verdicts import MIXED, KpiRow
from infra.guidance_history_paths import LOCAL_EDIT_MARK
from model_identity_fixtures import seed_identity_catalog, seed_manifest
from tests.plan_writer_authorizer import raw_plan_seed

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)
OPUS = "claude-opus-5-5"


@pytest.fixture(autouse=True)
def live_workflow_owners(monkeypatch):
    """Verdicts associate definitions through the shipped live owner rule, never stored metadata."""
    from core import workflow_verdict_read

    def owners(thread_id, _db):
        if thread_id != "owner":
            return {}
        return {"sdlc-process": SimpleNamespace(
            skill_dir=Path("/workflows/sdlc-process"), owner="owner", named=None,
        )}

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
         size: str | None = "M", created: str = "2026-10-01T00:00:00Z", ended: str = "2026-10-05T00:00:00Z") -> None:
    with raw_plan_seed(db):
        db.execute("INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,"
                   "commitment_id,workflow_ref,workflow_version,difficulty,size,requirements_revision,revision,"
                   "created_at,updated_at) VALUES (?,'plan',?,1,'task','task','done',?,?,?,?,?,?,1,1,?,?)",
                   (node_id, parent, state, node_id, workflow, version, difficulty, size, created, ended))
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
    """One owner message, answered after a turn working ``node_id``, with its classified fact."""
    db.execute("INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,plan_id,"
               "plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) "
               "VALUES (?,'root',0,'m','completed',?,'boot','plan',?,'attempt',?,'execution')",
               (f"turn-{message_id}", at[:9] + str(int(at[9]) - 1) + at[10:], node_id, node_id))
    db.execute("INSERT INTO messages (id,thread_id,role,content,created_at) VALUES (?,'root','user','x',?)",
               (message_id, at))
    db.execute("INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,attributes) "
               "VALUES (?,?,'measurement.correction','corsia','instrumented','2026-10-15','2026-10-15',?)",
               (f"fact-{message_id}", f"fact-{message_id}",
                json.dumps({"message_id": message_id, "label": label, "asset_version": counter})))


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
    assert (runs["failed"].terminal_at, runs["failed"].outcome) == ("2026-10-06T00:00:00Z", False)
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
    assert [(fact.message_id, fact.is_correction, fact.counter) for fact in facts] == [
        ("m1", True, "c1"), ("m2", False, "c1")]


def _definitions(db) -> None:
    event = {"attributes": {"pr": "id"}, "dedupe": ["pr"]}
    kpi = {"num": {"event": "qg.escape"}, "den": {"event": "qg.escape"}, "better": "lower", "can_decide": True,
           "coverage": {"of": "qg.escape", "by": "audit"}, "target": 0.1}
    rows = [("event", "qg.escape", 1, event, "2026-09-01T00:00:00Z"),
            ("kpi", "escapes", 1, {**kpi, "review": 0.2}, "2026-09-01T00:00:00Z"),
            ("kpi", "escapes", 2, {**kpi, "review": 0.5}, "2026-10-10T00:00:00Z")]
    for kind, name, version, spec, synced in rows:
        db.execute("INSERT INTO measure_definitions (owner_thread_id,kind,name,version,spec_json,file_path,"
                   "file_sha256,synced_at) VALUES ('owner',?,?,?,?,'/workflows/sdlc-process/measures.yaml','sha',?)",
                   (kind, name, version, json.dumps(spec), synced))


def test_the_frozen_plan_uses_the_definitions_in_force_at_the_versions_first_run(db) -> None:
    _definitions(db)
    node(db, "first", created="2026-10-01T00:00:00Z")
    node(db, "second", version="v2", created="2026-10-12T00:00:00Z", ended="2026-10-14T00:00:00Z")
    _, history = runs_of(read_snapshot(db, now=NOW))
    assert history.owner == "owner"
    (v1,) = [m for m in freeze_plan(history, "v1", NOW).measures if m.source == "kpi"]
    (v2,) = [m for m in freeze_plan(history, "v2", NOW).measures if m.source == "kpi"]
    assert (v1.definition_version, v1.margin_gap) == (1, pytest.approx(0.1))
    assert (v2.definition_version, v2.margin_gap) == (2, pytest.approx(0.4))


def test_kpi_rows_come_only_from_an_injected_reader(db) -> None:
    node(db, "run")
    runs, _ = runs_of(read_snapshot(db, now=NOW))
    assert runs["run"].kpi_rows is None  # no formula engine yet: deciding KPIs read Not comparable
    row = KpiRow("num", "observed", 1.0, "2026-10-05T00:00:00Z")
    read = read_snapshot(db, now=NOW, kpi_reader=lambda _db, found, _defs: {r.run_id: {"escapes": (row,)} for r in found})
    assert runs_of(read)[0]["run"].kpi_rows == {"escapes": (row,)}


def test_cost_is_the_accepted_chain_price_and_unknown_never_zero_otherwise(db) -> None:
    node(db, "priced")
    usage(db, "u1", "priced")
    node(db, "nested")
    usage(db, "u2", "nested")
    node(db, "child", parent="nested", workflow=None, ended="2026-10-04T00:00:00Z")
    usage(db, "u3", "child")
    node(db, "half")
    usage(db, "u4", "half")
    node(db, "broke", parent="half", workflow=None, state="failed")
    node(db, "failed", state="failed")
    usage(db, "u5", "failed")
    priced = price_snapshot(db, read_snapshot(db, now=NOW))
    runs, _ = runs_of(priced)
    from core.workflow_verdict_read import history_grain
    params = {"user_id": "steve", "scope_project": "all", "scope_agent": None,
              "grain": history_grain(datetime(2026, 10, 1).date(), NOW)}
    by_task = {row["task_id"]: row["dollars"] for row in query_model_costs(db, params, IdentityResolver(db))}
    assert runs["priced"].cost == pytest.approx(by_task["priced"]) and by_task["priced"] > 0
    assert runs["nested"].cost == pytest.approx(by_task["nested"] + by_task["child"])
    assert runs["half"].cost is None and runs["failed"].cost is None
    assert db.execute("SELECT COUNT(*) FROM temp.measurement_task_chain").fetchone()[0] == 0


def test_calibration_script_reads_a_fixture_read_only_and_prints_aggregates_only(db, isolated_db, capsys) -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "verdict_calibration.py"
    spec = importlib.util.spec_from_file_location("verdict_calibration", path)
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
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
    assert "n=5 p50=1.00" in out and "v1 -> v2" in out and "no_baseline" in out
    assert not any(f"r{index}" in out.split() or f"m{index}" in out for index in range(66))


def test_read_snapshot_never_writes_so_it_runs_on_the_query_only_lane(db) -> None:
    node(db, "run")
    usage(db, "u1", "run")
    correction(db, "m1", "run")
    db.commit()
    db.execute("PRAGMA query_only=ON")
    runs, _ = runs_of(read_snapshot(db, now=NOW))
    assert runs["run"].group[0] == OPUS
