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


OPTIONAL_STEPS = (
    "    - {id: draft, required: true, check: Draft exists, difficulty: {default: routine}}\n"
    "    - {id: polish, required: false, after: [draft], check: Polished, difficulty: {default: routine}}\n"
)


async def _waited_on_from_another_plan(tmp_path, monkeypatch, steps):
    """A run in one thread's plan, and a task in another thread's plan that waits on it."""
    from plans import dependencies, service
    from tests.event_routing_helpers import mk_session

    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, steps)})
    started = await run.start("ship")
    phase = started["node"]["id"]
    other = mk_session("run-end-dependent-owner")
    db = get_db()
    try:
        plan = service.ensure_plan("thread", other, db=db)
        task = service.add_node(
            plan.id, kind="task", title="Publish the notes", acceptance_criteria="Published",
            provenance="test.run_end", db=db,
        ).id
        db.commit()
        dependencies.add_dependency(task, phase, provenance="test.run_end", db=db)
        db.commit()
    finally:
        db.close()
    return run, phase, [step["id"] for step in started["steps"]], other, task


def _wakes(run, thread_id):
    return [row["id"] for row in run.rows(
        "SELECT id FROM messages WHERE thread_id=? AND id LIKE 'plan-wake:prerequisite_settled:%'"
        " ORDER BY input_order",
        thread_id,
    )]


def _blocked_events(run, task):
    return [row["event"] for row in run.rows(
        "SELECT event FROM plan_node_events WHERE node_id=? AND event LIKE 'blocked_%' ORDER BY rowid", task,
    )]


async def _done(run, node):
    done = await run.update("mark_done", _args(run, node, evidence="done", verification_result="passed"))
    assert not isinstance(done, str), done
    return done


async def test_a_run_in_another_plan_tells_its_dependent_once_when_it_completes(isolated_db, tmp_path, monkeypatch):
    run, phase, (draft, review), other, task = await _waited_on_from_another_plan(tmp_path, monkeypatch, STEPS)

    await _done(run, draft)
    assert _wakes(run, other) == []
    await _done(run, review)

    wakes = _wakes(run, other)
    assert len(wakes) == 1 and wakes[0].endswith(f":{phase}:done")
    assert _causes(task) == ([], {})
    assert _blocked_events(run, task) == ["blocked_raised", "blocked_cleared"]


async def test_accepting_a_runs_last_step_with_a_follow_up_does_not_end_it(isolated_db, tmp_path, monkeypatch):
    """The follow-up is the rest of the step: nothing is told until it settles (review of #3182)."""
    run, phase, (draft, review), other, task = await _waited_on_from_another_plan(tmp_path, monkeypatch, STEPS)
    await _done(run, draft)
    activated = await run.update("activate", _args(run, review))
    assert not isinstance(activated, str), activated
    submitted = await run.update("request_review", _args(run, review, evidence="Reviewed"))
    assert not isinstance(submitted, str), submitted

    followed = await run.update("accept_with_follow_up", _args(
        run, review, verification_result="passed", title="Fix the typo",
        acceptance_criteria="Typo fixed", size="XS",
    ))
    assert not isinstance(followed, str), followed

    assert _wakes(run, other) == []
    assert [cause["reason_key"] for cause in _causes(task)[0]] == ["dependency"]
    assert _blocked_events(run, task) == ["blocked_raised"]

    follow_up = run.rows(
        "SELECT id FROM plan_nodes WHERE follow_up_of=? ORDER BY created_at", review,
    )[0]["id"]
    await _done(run, follow_up)

    wakes = _wakes(run, other)
    assert len(wakes) == 1 and wakes[0].endswith(f":{phase}:done")
    assert _causes(task) == ([], {})


async def test_archiving_a_runs_last_open_optional_step_ends_it_and_tells_its_dependent(
    isolated_db, tmp_path, monkeypatch,
):
    run, phase, (draft, polish), other, task = await _waited_on_from_another_plan(
        tmp_path, monkeypatch, OPTIONAL_STEPS,
    )
    await _done(run, draft)
    assert [cause["reason_key"] for cause in _causes(task)[0]] == ["dependency"]

    archived = await run.update("archive_node", _args(run, polish))
    assert not isinstance(archived, str), archived

    wakes = _wakes(run, other)
    assert len(wakes) == 1 and wakes[0].endswith(f":{phase}:done")
    assert _causes(task) == ([], {})
    assert _blocked_events(run, task) == ["blocked_raised", "blocked_cleared"]


async def test_archiving_a_finished_runs_steps_keeps_its_dependent_free(isolated_db, tmp_path, monkeypatch):
    """A run's end is recorded once; cleaning up its steps afterwards does not undo it (review of #3182)."""
    run, _phase, (draft, review), other, task = await _waited_on_from_another_plan(tmp_path, monkeypatch, STEPS)
    for step in (draft, review):
        await _done(run, step)
    assert _causes(task) == ([], {})

    for step in (review, draft):
        archived = await run.update("archive_node", _args(run, step))
        assert not isinstance(archived, str), archived

    assert _causes(task) == ([], {})
    assert _blocked_events(run, task) == ["blocked_raised", "blocked_cleared"]
    assert len(_wakes(run, other)) == 1
