"""A declared type a rule's run emits on its artifact reaches that artifact's registered owner (G5).

Rule 0 routes it as it routes any event on the artifact, unless its declaration says
``default_delivery: none``; an agent's or a schedule's own emit still goes to its declarer.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from automation import script_schedules
from events.identity import pr_external_id
from events.intake import admit_event
from infra.database import get_db
from infra.measure_registry import REGISTRY, sync
from plans.boundary import PlanActor
from tests.event_routing_helpers import mk_session, register_pr
from tests.test_reviewer_verdict_resolves_watch import REPO, _armed, _watching_verdict

DECLARER, PR_OWNER = "qg-declarer", "owner"
FACTS = {"verdict": "clean"}


def _declare(db, tmp_path: Path, *, default_delivery: str | None = None):
    REGISTRY.reset()
    mk_session(DECLARER, project_id="project-a")
    mk_session(PR_OWNER, project_id="project-a")
    text = ("schema: corsia.measures/1\nnamespace: reviewer\nevents:\n  reviewer.verdict:\n"
            "    attributes: {verdict: id}\n    dedupe: [verdict]\n    use: route\n"
            + (f"    default_delivery: {default_delivery}\n" if default_delivery else ""))
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
