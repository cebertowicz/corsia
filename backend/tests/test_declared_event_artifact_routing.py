"""A declared type a rule's run emits on its artifact reaches that artifact's registered owner (G5).

Rule 0 routes it as it routes any event on the artifact, unless its declaration says
``default_delivery: none``; an agent's or a schedule's own emit still goes to its declarer.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from automation import script_schedules
from events.identity import pr_external_id
from events.intake import admit_event
from infra.database import get_db
from infra.measure_registry import REGISTRY, sync
from plans import service
from plans.boundary import PlanActor, mutate
from tests.event_routing_helpers import mk_session, register_pr

REPO = "owner/repo"
DECLARER, PR_OWNER = "qg-declarer", "owner"
FACTS = {"verdict": "clean"}


def _artifact_id(db, external_id: str) -> str:
    from registry import artifact_record

    return str(artifact_record.identity_row_for(external_id, db=db)["id"])


def _watching_verdict(db, actor, *, external_id: str):
    """A task of the owner's, waiting on the PR's declared verdict event."""
    plan = service.ensure_plan("thread", actor.thread_id, db=db)
    node = service.add_node(
        plan.id, kind="task", title="Await verdict",
        acceptance_criteria="The verdict resolves this wait", db=db,
    )
    service.transition_node(node.id, "active", db=db)
    db.commit()
    mutate(
        plan.id, "arm_watch",
        {
            "node_id": node.id,
            "condition": {"type": "event", "value": "reviewer.verdict"},
            "artifact_id": _artifact_id(db, external_id),
            "expected_event": "reviewer.verdict",
            "expected_revision": service._node_row(db, node.id)["revision"],
        },
        actor=actor, provenance="test.verdict_watch",
        mutation_id=uuid.uuid4().hex, db=db,
    )
    db.commit()
    return plan, node


def _armed(db, node_id: str) -> int:
    return int(db.execute(
        "SELECT COUNT(*) FROM expected_event_watches "
        "WHERE plan_node_id=? AND status='armed'",
        (node_id,),
    ).fetchone()[0])


def _declare(db, tmp_path: Path, *, default_delivery: str | None = None, event_class: str | None = None):
    REGISTRY.reset()
    mk_session(DECLARER, project_id="project-a")
    mk_session(PR_OWNER, project_id="project-a")
    text = ("schema: corsia.measures/1\nnamespace: reviewer\nevents:\n  reviewer.verdict:\n"
            "    attributes: {verdict: id}\n    dedupe: [verdict]\n    use: route\n"
            + (f"    default_delivery: {default_delivery}\n" if default_delivery else "")
            + (f"    event_class: {event_class}\n" if event_class else ""))
    folder = tmp_path / ".threads" / DECLARER
    folder.mkdir(parents=True)
    (folder / "measures.yaml").write_text(text)
    sync(db, owner_thread_id=DECLARER, path=str(folder / "measures.yaml"), text=text)
    path = tmp_path / "publish.py"
    path.write_text("print('publish')")
    schedule = script_schedules.create_script(
        DECLARER, "project", script_path=path, script_sha256=script_schedules.file_sha256(path),
        cadence_seconds=600, series=[], timeout_seconds=30,
        now="2026-01-01T00:00:00+00:00", db=db,
    )
    db.commit()
    return schedule


def _running_rule_run(db, schedule, *, pr: str | None = REPO + "#7") -> int:
    """The decision row of the declarer's ``run_script`` rule, running on a PR."""
    db.execute(
        'INSERT INTO event_rules (id,created_at,updated_at,scope,connector,event_type,"match",action,"to",'
        'origin,created_by,authority_ref,script_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ("publish-rule", "t", "t", "project:project-a", "worker", "result", "[]", "run_script",
         f"thread:{DECLARER}", f"agent:{DECLARER}", DECLARER, "self", schedule.id),
    )
    cursor = db.execute(
        "INSERT INTO event_route_decisions (at,event_id,connector,event_type,watch_event,artifact_integration,"
        "artifact_type,artifact_external_id,rule_id,thread_id,outcome,via) VALUES "
        "('t','req-1','worker','result','result','github','pull-request',?,?,?,'running','rule')",
        (pr_external_id(REPO, 7) if pr else None, "publish-rule", DECLARER),
    )
    db.commit()
    return cursor.lastrowid


def _emit(schedule, decision: int | None, event_id: str = "verdict-1"):
    return asyncio.run(admit_event(
        event_type="reviewer.verdict", facts=FACTS, origin="emitter", producer=f"script:{schedule.id}",
        event_id=event_id, rule_decision_id=decision,
    ))


def _digested(db, thread: str, event_id: str = "verdict-1") -> int:
    return db.execute(
        "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='thread_context' AND object_id=? "
        "AND json_extract(details_json,'$.event_id') LIKE ?", (thread, event_id + "%"),
    ).fetchone()[0]


@pytest.fixture
def db(isolated_db):
    return get_db()


def test_a_verdict_a_rule_run_emits_reaches_the_prs_owner_and_ends_its_watch(db, tmp_path):
    schedule = _declare(db, tmp_path)
    register_pr(REPO, 7, PR_OWNER)
    _plan, node = _watching_verdict(db, PlanActor(PR_OWNER, "user-1", "project-a"), external_id=pr_external_id(REPO, 7))
    assert _armed(db, node.id) == 1
    decision = _running_rule_run(db, schedule)

    assert _emit(schedule, decision).accepted
    assert (_digested(db, PR_OWNER), _digested(db, DECLARER)) == (1, 0)
    assert _armed(db, node.id) == 0


def test_a_verdict_on_an_artifact_with_no_owner_is_recorded_and_reaches_no_one(db, tmp_path):
    schedule = _declare(db, tmp_path)
    decision = _running_rule_run(db, schedule)

    result = _emit(schedule, decision)
    assert result.accepted and not result.refusal
    assert (_digested(db, PR_OWNER), _digested(db, DECLARER)) == (0, 0)
    assert db.execute("SELECT COUNT(*) FROM dead_letter_events").fetchone()[0] == 0
    assert db.execute("SELECT outcome FROM event_intake_records WHERE event_id='verdict-1'").fetchone()[0] == "accepted"


def test_default_delivery_none_leaves_the_verdict_to_rules(db, tmp_path):
    schedule = _declare(db, tmp_path, default_delivery="none")
    register_pr(REPO, 7, PR_OWNER)
    decision = _running_rule_run(db, schedule)

    assert _emit(schedule, decision).accepted
    assert (_digested(db, PR_OWNER), _digested(db, DECLARER)) == (0, 0)

    db.execute(
        'INSERT INTO event_rules (id,created_at,updated_at,scope,connector,event_type,"match",action,"to",mode,'
        'origin,created_by,authority_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ("owner-verdicts", "t", "t", "project:project-a", "reviewer", "verdict", "[]", "deliver",
         "registered_owner", "digest", f"agent:{PR_OWNER}", PR_OWNER, "self"),
    )
    db.commit()
    assert _emit(schedule, decision, "verdict-2").accepted
    assert (_digested(db, PR_OWNER, "verdict-2"), _digested(db, DECLARER, "verdict-2")) == (1, 0)


def test_an_emit_no_rule_run_made_still_goes_to_its_declarer(db, tmp_path):
    schedule = _declare(db, tmp_path)
    register_pr(REPO, 7, PR_OWNER)

    assert _emit(schedule, None).accepted
    assert (_digested(db, DECLARER), _digested(db, PR_OWNER)) == (1, 0)


def test_default_delivery_must_be_owner_or_none(db, tmp_path):
    folder = tmp_path / ".threads" / DECLARER
    folder.mkdir(parents=True)
    mk_session(DECLARER, project_id="project-a")
    text = ("schema: corsia.measures/1\nnamespace: reviewer\nevents:\n  reviewer.verdict:\n"
            "    attributes: {verdict: id}\n    dedupe: [verdict]\n    use: route\n    default_delivery: all\n")
    (folder / "measures.yaml").write_text(text)
    with pytest.raises(ValueError, match="default_delivery must be owner or none"):
        sync(db, owner_thread_id=DECLARER, path=str(folder / "measures.yaml"), text=text)


def _deliver_rule(db, rule_id: str, *, writer: str, to: str, mode: str) -> None:
    db.execute(
        'INSERT INTO event_rules (id,created_at,updated_at,scope,connector,event_type,"match",action,"to",mode,'
        'origin,created_by,authority_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (rule_id, "t", "t", "project:project-a", "reviewer", "verdict", "[]", "deliver", to, mode,
         f"agent:{writer}", writer, "self"),
    )
    db.commit()


def _woken(db, thread: str) -> int:
    return db.execute("SELECT COUNT(*) FROM messages WHERE thread_id=? AND content LIKE '[EVENT: verdict]%'",
                      (thread,)).fetchone()[0]


def test_a_wake_rule_the_owner_wrote_wakes_it_once_and_ends_its_watch(db, tmp_path):
    schedule = _declare(db, tmp_path)
    register_pr(REPO, 7, PR_OWNER)
    _plan, node = _watching_verdict(db, PlanActor(PR_OWNER, "user-1", "project-a"), external_id=pr_external_id(REPO, 7))
    _deliver_rule(db, "owner-wake", writer=PR_OWNER, to="registered_owner", mode="wake")
    decision = _running_rule_run(db, schedule)

    assert _emit(schedule, decision).accepted
    assert (_woken(db, PR_OWNER), _digested(db, PR_OWNER), _woken(db, DECLARER)) == (1, 0, 0)
    assert db.execute("SELECT COUNT(*) FROM event_route_decisions WHERE event_id='verdict-1' AND thread_id=?",
                      (PR_OWNER,)).fetchone()[0] == 1
    assert _armed(db, node.id) == 0


def test_an_outside_pr_has_no_owner_even_for_the_wake_rule(db, tmp_path):
    schedule = _declare(db, tmp_path)
    _deliver_rule(db, "qg-wake", writer=DECLARER, to="registered_owner", mode="wake")
    decision = _running_rule_run(db, schedule)

    assert _emit(schedule, decision).accepted
    assert [(_woken(db, t), _digested(db, t)) for t in (PR_OWNER, DECLARER)] == [(0, 0), (0, 0)]
    assert db.execute("SELECT COUNT(*) FROM dead_letter_events").fetchone()[0] == 0
    assert db.execute("SELECT outcome FROM event_intake_records WHERE event_id='verdict-1'").fetchone()[0] == "accepted"
    # The rule resolves to nobody and says so; it is not the declarer's.
    assert [tuple(r) for r in db.execute(
        "SELECT thread_id, outcome, reason FROM event_route_decisions WHERE rule_id='qg-wake'",
    )] == [(None, "dead_lettered", "no registered owner")]


def test_an_added_delivery_to_a_third_thread_ends_its_dotted_watch(db, tmp_path):
    schedule = _declare(db, tmp_path)
    mk_session("third", project_id="project-a")
    register_pr(REPO, 7, "third")
    _plan, node = _watching_verdict(db, PlanActor("third", "user-1", "project-a"), external_id=pr_external_id(REPO, 7))
    db.execute("UPDATE artifact_registrations SET parent_thread_id=? WHERE parent_thread_id='third'", (PR_OWNER,))
    db.commit()
    _deliver_rule(db, "third-digest", writer="third", to="thread:third", mode="digest")
    decision = _running_rule_run(db, schedule)

    assert _armed(db, node.id) == 1
    assert _emit(schedule, decision).accepted
    assert _digested(db, "third") == 1 and _armed(db, node.id) == 0


def _owner_with_watch(db):
    register_pr(REPO, 7, PR_OWNER)
    _plan, node = _watching_verdict(db, PlanActor(PR_OWNER, "user-1", "project-a"), external_id=pr_external_id(REPO, 7))
    return node


def _delivery_rows(db, thread: str) -> int:
    return db.execute("SELECT COUNT(*) FROM event_route_decisions WHERE event_id='verdict-1' AND thread_id=?",
                      (thread,)).fetchone()[0]


def test_an_actionable_declared_verdict_wakes_the_owner_once_with_no_rule(db, tmp_path):
    schedule = _declare(db, tmp_path, event_class="actionable")
    node = _owner_with_watch(db)

    assert _emit(schedule, _running_rule_run(db, schedule)).accepted
    assert (_woken(db, PR_OWNER), _digested(db, PR_OWNER), _woken(db, DECLARER)) == (1, 0, 0)
    assert _armed(db, node.id) == 0  # rule 0 writes no rule row; the one wake message is the delivery


def test_an_informational_declared_verdict_only_digests(db, tmp_path):
    schedule = _declare(db, tmp_path, event_class="informational")
    _owner_with_watch(db)

    assert _emit(schedule, _running_rule_run(db, schedule)).accepted
    assert (_woken(db, PR_OWNER), _digested(db, PR_OWNER)) == (0, 1)


def test_a_declarers_wake_rule_rides_an_actionable_verdict_as_one_delivery(db, tmp_path):
    schedule = _declare(db, tmp_path, event_class="actionable")
    _owner_with_watch(db)
    _deliver_rule(db, "qg-wake", writer=DECLARER, to="registered_owner", mode="wake")

    assert _emit(schedule, _running_rule_run(db, schedule)).accepted
    assert (_woken(db, PR_OWNER), _digested(db, PR_OWNER)) == (1, 0)
    assert _delivery_rows(db, PR_OWNER) == 1


@pytest.mark.parametrize(("extra", "message"), [
    ("use: record\n    event_class: actionable", "event_class needs use: route or both"),
    ("use: record\n    default_delivery: none", "default_delivery needs use: route or both"),
    ("use: route\n    event_class: urgent", "event_class must be actionable or informational"),
])
def test_routing_fields_are_refused_on_a_record_type_and_for_an_unknown_value(db, tmp_path, extra, message):
    folder = tmp_path / ".threads" / DECLARER
    folder.mkdir(parents=True)
    mk_session(DECLARER, project_id="project-a")
    text = ("schema: corsia.measures/1\nnamespace: reviewer\nevents:\n  reviewer.verdict:\n"
            f"    attributes: {{verdict: id}}\n    dedupe: [verdict]\n    {extra}\n")
    (folder / "measures.yaml").write_text(text)
    with pytest.raises(ValueError, match=message):
        sync(db, owner_thread_id=DECLARER, path=str(folder / "measures.yaml"), text=text)
