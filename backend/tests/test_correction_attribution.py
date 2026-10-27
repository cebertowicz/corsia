"""Which plan node a classified owner message corrects: the answered turn's, from real rows."""
from __future__ import annotations

import json

import pytest

from core import correction_attribution
from core.correction_attribution import NOT_IN_WORKFLOW, UNATTRIBUTED, attributed_facts, workflow_run_of
from core.corrections_recording import ClassifierVersion, CorrectionCandidate, SOURCE_KIND_MESSAGE
from jobs.corrections_recording import _record_fact
from infra.measure_validation import deciding_refusal
from tests.plan_writer_authorizer import raw_plan_seed


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    connection.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) "
                       "VALUES ('plan','thread','t0','active',1,'t','t')")
    for thread in ("t0", "t1", "t2", "t3", "t4"):
        connection.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?,'steve','x','t','t')",
                           (thread,))
    with raw_plan_seed(connection):
        for node in ("earlier", "own", "attempt"):
            connection.execute("INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,"
                               "commitment_id,requirements_revision,revision,created_at,updated_at) "
                               "VALUES (?,'plan',1,'task','x','x','active',?,1,1,'t','t')", (node, node))
        connection.execute("INSERT INTO plan_node_attempts (id,plan_id,node_id,executor_thread_id,requirements_revision,"
                           "attempt_ordinal,provenance,selected_at) VALUES "
                           "('a1','plan','attempt','t3',1,1,'test','2026-10-01 00:00:00')")
    try:
        yield connection
    finally:
        connection.close()


def turn(db, turn_id: str, thread: str, node: str, started: str) -> None:
    db.execute("INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,plan_id,"
               "plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) "
               "VALUES (?,?,0,'m','completed',?,'boot','plan',?,'a',?,'execution')", (turn_id, thread, started, node, node))


def message(db, message_id: str, thread: str, at: str, *, own_turn: str | None = None) -> None:
    db.execute("INSERT INTO messages (id,thread_id,role,content,created_at,active_turn_id) VALUES (?,?,'user','x',?,?)",
               (message_id, thread, at, own_turn))


def legacy_fact(db, message_id: str, *, label: str = "correction", retracted: bool = False) -> None:
    """The actual pre-source-kind attributes emitted by the historical writer."""
    db.execute("INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,"
               "attributes,retracted_at) VALUES (?,?,'measurement.correction','corsia','instrumented','2026-10-15',"
               "'2026-10-15',?,?,?)",
               (f"legacy-{message_id}", f"legacy-{message_id}", f"message:{message_id}",
                json.dumps({"message_id": message_id, "thread_id": "ignored", "label": label, "asset_version": "c1"}),
                "2026-10-16" if retracted else None))


def record_current_fact(db, message_id: str, thread: str, at: str) -> None:
    """Write the current source-kind shape through the production fact writer."""
    db.commit()
    candidate = CorrectionCandidate(SOURCE_KIND_MESSAGE, message_id, thread, None, "x", at)
    payload = {"label": "correction", "truncated": False, "original_tokens": 1, "failure_reason": "none"}
    version = ClassifierVersion("asset", "current", "head", 0.5, "policy")
    assert _record_fact(candidate, payload, version) is True


def test_only_the_answered_turn_counts_by_the_source_time_never_the_fact_time(db) -> None:
    turn(db, "before", "t1", "earlier", "2026-10-02T00:00:00Z")
    turn(db, "mine", "t1", "own", "2026-10-03 00:00:00")  # the message's own turn never counts as earlier
    message(db, "m1", "t1", "2026-10-03T00:00:01Z", own_turn="mine")
    turn(db, "only", "t2", "own", "2026-10-03T00:00:00Z")
    message(db, "m2", "t2", "2026-10-02T00:00:00Z", own_turn="only")  # the reply turn's task is not inferred
    message(db, "m3", "t3", "2026-10-02T00:00:00Z")  # nor the attempt open when it arrived
    db.execute("INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
               "VALUES ('idle','t4',0,'m','completed','2026-10-01T00:00:00Z','boot')")
    message(db, "m4", "t4", "2026-10-02T00:00:00Z")  # answered a turn between tasks
    message(db, "gone", "t4", "2026-10-02T00:00:00Z")
    message(db, "noise", "t4", "2026-10-02T00:00:00Z")
    for message_id in ("m1", "m2", "m3", "m4"):
        legacy_fact(db, message_id)
    legacy_fact(db, "gone", retracted=True)
    legacy_fact(db, "noise", label="unreadable")
    found = {fact.source_id: fact.node_id for fact in attributed_facts(db)}
    assert found == {"m1": "earlier", "m2": None, "m3": None, "m4": None}


def test_source_key_joins_use_message_and_comment_primary_keys(db) -> None:
    """The source key is canonical without turning attribution into table scans."""
    plan = db.execute(
        f"EXPLAIN QUERY PLAN {correction_attribution._ATTRIBUTED_SQL}",
        {"event": "measurement.correction", "correction": "correction", "not_correction": "not_correction"},
    ).fetchall()

    details = [str(row["detail"]) for row in plan]
    assert not any("SCAN m" in detail or "SCAN c" in detail for detail in details)


def test_current_writer_and_legacy_attributes_both_reach_their_workflow_run(db) -> None:
    turn(db, "legacy-turn", "t1", "earlier", "2026-10-02T00:00:00Z")
    turn(db, "current-turn", "t2", "own", "2026-10-02T00:00:00Z")
    message(db, "legacy", "t1", "2026-10-02T00:00:01Z")
    message(db, "current", "t2", "2026-10-02T00:00:01Z")
    legacy_fact(db, "legacy")
    record_current_fact(db, "current", "t2", "2026-10-02T00:00:01Z")

    facts = {fact.source_id: fact for fact in attributed_facts(db)}
    assert {(source_id, fact.source_kind, fact.node_id) for source_id, fact in facts.items()} == {
        ("legacy", SOURCE_KIND_MESSAGE, "earlier"), ("current", SOURCE_KIND_MESSAGE, "own"),
    }
    assert {source_id: workflow_run_of(fact.node_id, {"earlier": "run", "own": "run"}, ["run"])
            for source_id, fact in facts.items()} == {"legacy": "run", "current": "run"}


def test_missing_source_arrival_time_stays_unattributed(db) -> None:
    legacy_fact(db, "missing-source")

    (fact,) = attributed_facts(db)
    assert (fact.source_kind, fact.source_id, fact.source_at, fact.node_id) == (
        SOURCE_KIND_MESSAGE, "missing-source", None, None,
    )


def test_a_node_maps_to_its_nearest_run_or_says_why_not() -> None:
    parents = {"step": "run", "run": "root", "root": None, "loose": None}
    assert workflow_run_of("step", parents, ["run"]) == "run"
    assert workflow_run_of("loose", parents, ["run"]) == NOT_IN_WORKFLOW
    assert workflow_run_of(None, parents, ["run"]) == UNATTRIBUTED


def test_a_self_caught_fact_has_no_classifier_counter_and_is_not_a_persons_correction(db) -> None:
    from core.corrections_recording import ACTIVE_VERSION_SETTING, version_key
    from core.correction_attribution import person_corrections
    from infra.settings import set_setting

    turn(db, "before", "t1", "earlier", "2026-10-02T00:00:00Z")
    message(db, "m1", "t1", "2026-10-03T00:00:00Z")
    record_current_fact(db, "m1", "t1", "2026-10-03T00:00:00Z")
    db.execute(
        "INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,"
        "agent_thread_id,attributes) VALUES ('sc','sc','measurement.correction','corsia','instrumented',"
        "'2026-10-03','2026-10-03','self-caught:plan-failed:n1','t1',?)",
        (json.dumps({"source_kind": "self_caught", "source_id": "plan-failed:n1", "thread_id": "t1",
                     "label": "correction", "recorded_at": "2026-10-03T00:00:00Z"}),),
    )
    set_setting(ACTIVE_VERSION_SETTING, {"key": version_key(ClassifierVersion("asset", "current", "head", 0.5, "policy"))}, db=db)
    db.commit()

    counters = {fact.source_kind: fact.counter for fact in attributed_facts(db)}

    assert counters["self_caught"] is None
    assert counters["message"] is not None
    assert person_corrections(db, "2000-01-01 00", "2100-01-01 00") == [("2026-10-03T00:00:00Z", "t1")]


def test_deciding_refusal_reruns_the_sync_test_when_a_verdict_is_read() -> None:
    events = {"qg.escape": {"attributes": {"pr": "id"}}}
    kpi = {"num": {"event": "qg.escape"}, "den": {"event": "qg.escape"}, "can_decide": True,
           "target": 0.1, "review": 0.2, "coverage": {"of": "qg.escape", "by": "audit"}}
    assert deciding_refusal("escapes", kpi, events, "sdlc-process") is None
    missing = {key: value for key, value in kpi.items() if key != "review"}
    assert "review is missing" in (deciding_refusal("escapes", missing, events, "sdlc-process") or "")
    assert "version" in (deciding_refusal("escapes", kpi, events, None) or "")
    assert "version_attr" in (deciding_refusal("escapes", kpi, events, "sdlc-process", "nope") or "")
