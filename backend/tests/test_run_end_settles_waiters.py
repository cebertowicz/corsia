"""A workflow run's end settles what waits on it (#3145).

The run's phase keeps its stored state when the run ends (its record is the
``workflow_run_ended`` event), so a dependency on the run reads that record:
completed meets it, failed is a prerequisite that will never be met.
"""

import pytest

from infra.database import get_db
from plans import admission, cross_plan, node_waits
from tests.test_workflow_runs import Run, SHIP_BODY

pytestmark = pytest.mark.asyncio

STEPS = (
    "    - {id: draft, required: true, check: Draft exists, difficulty: {default: routine}}\n"
    "    - {id: review, required: true, after: [draft], check: Review passes, difficulty: {default: routine}}\n"
)


def _args(run, node, **extra):
    return {"node_id": node, "expected_revision": run.revision(node), **extra}


async def _waiting_on_a_run(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, STEPS)})
    started = await run.start("ship")
    phase = started["node"]["id"]
    draft, review = (step["id"] for step in started["steps"])
    added = await run.update("add", {
        "title": "Publish the notes", "acceptance_criteria": "Published", "size": "XS", "intent": "Publish",
    })
    task = added["node"]["id"]
    edge = await run.update("add_dependency", _args(run, task, depends_on_node_id=phase))
    assert not isinstance(edge, str), edge
    return run, phase, draft, review, task


def _causes(task):
    db = get_db()
    try:
        return admission.project(task, db=db)["causes"], node_waits.unmet_prerequisites(db, [task])
    finally:
        db.close()


def _settled(monkeypatch):
    told = []
    original = cross_plan.prerequisite_settled
    monkeypatch.setattr(
        cross_plan, "prerequisite_settled",
        lambda connection, node_id, **kw: (told.append(node_id), original(connection, node_id, **kw)),
    )
    return told


async def test_a_completed_run_meets_the_dependency_on_it(isolated_db, tmp_path, monkeypatch):
    run, phase, draft, review, task = await _waiting_on_a_run(tmp_path, monkeypatch)
    told = _settled(monkeypatch)
    causes, _ = _causes(task)
    assert [cause["reason_key"] for cause in causes] == ["dependency"]

    for step in (draft, review):
        done = await run.update("mark_done", _args(run, step, evidence="done", verification_result="passed"))
        assert not isinstance(done, str), done

    assert run.rows("SELECT state FROM plan_nodes WHERE id=?", phase) == [{"state": "pending"}]
    causes, unmet = _causes(task)
    assert causes == [] and unmet == {}
    assert phase in told
    assert [row["event"] for row in run.rows(
        "SELECT event FROM plan_node_events WHERE node_id=? AND event LIKE 'blocked_%' ORDER BY rowid", task,
    )] == ["blocked_raised", "blocked_cleared"]


async def test_a_failed_run_is_a_failed_prerequisite(isolated_db, tmp_path, monkeypatch):
    run, phase, draft, _review, task = await _waiting_on_a_run(tmp_path, monkeypatch)
    told = _settled(monkeypatch)

    failed = await run.update("fail", _args(run, draft))
    assert not isinstance(failed, str), failed

    assert run.rows("SELECT state FROM plan_nodes WHERE id=?", phase) == [{"state": "pending"}]
    causes, _ = _causes(task)
    assert [cause["reason_key"] for cause in causes] == ["dependency_failed"]
    assert phase in told


async def test_a_run_still_open_keeps_its_dependent_waiting(isolated_db, tmp_path, monkeypatch):
    run, _phase, draft, _review, task = await _waiting_on_a_run(tmp_path, monkeypatch)

    await run.update("mark_done", _args(run, draft, evidence="done", verification_result="passed"))

    causes, _ = _causes(task)
    assert [cause["reason_key"] for cause in causes] == ["dependency"]
