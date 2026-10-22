"""A trial lesson's review is a pending task scheduled for its review date.

- It is created once per lesson version on the owner's plan, never started and
  never selected, so it takes no executor slot before its date.
- At the date the ordinary scheduled wake names it. A paused owner is not
  woken; the wait stays armed and the wake arrives once the user resumes.
- A named wake is not guaranteed. When it stalls, the review is left pending
  with no wait, as ready work the liveness wake carries by node id.
- A capture and a lesson with the same id and hash keep separate reviews.
  Appended evidence, and a retry, create nothing.
- A new version, a capture that became a trial, and a change of owner each
  close the review they replace in the same write. A missing owner closes
  nothing.
- Only an open review is found again. A version or an owner that comes back,
  and a version placed again after its review was settled, each get a new
  review under the same key: one open review, and never a settled one returned.
- The same version placed again with another review date keeps its review and
  moves its wake to that date: one wait armed, none added for the same date.
- Only a pending, unselected review is moved. One its owner started or handed
  for review, or one with an open attempt, keeps its waits and its attempt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from automation.continuation import runner
from automation.continuation.sources.liveness import LivenessSource
from automation.continuation.sources.watch_expiry import WatchExpirySource
from infra.database import write_transaction
from infra.schema import SCHEMA_SQL
from plans import admission, admission_attempts, lesson_reviews, scheduled_wait, selection_validity, service
from plans.projections import liveness_signal
from plans.boundary import PlanActor, mutate
from tests.plan_writer_authorizer import raw_plan_seed

LESSON = "0f3a6f0e-7f0e-5a55-9d40-6f1f0e6f7a11"
SHA = "a" * 64
OTHER_SHA = "b" * 64
ACCEPTANCE = "The trial is retired, promoted or extended, with the evidence."


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')"
    )
    for thread in ("owner", "other"):
        _thread(connection, thread)
    _artifact(connection, "artifact")
    connection.commit()
    yield connection
    connection.close()


def _thread(db, thread_id: str) -> None:
    db.execute(
        """INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
           VALUES (?,'user','project','test/model','t','t')""",
        (thread_id,),
    )


def _artifact(db, artifact_id: str) -> None:
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,title,state,created_at,updated_at)
           VALUES (?,'workspace','skill',?,?,'active','t','t')""",
        (artifact_id, f"workspace://{artifact_id}", artifact_id),
    )


def _due(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).replace(microsecond=0).isoformat()


def _lesson(db, owner: str = "owner", *, sha: str = SHA, lesson_id: str = LESSON, due=None):
    result = lesson_reviews.ensure_lesson_review(
        db, owner, lesson_id=lesson_id, behavioral_sha256=sha, artifact_id="artifact",
        title="Review trial lesson: cite-the-head", acceptance=ACCEPTANCE,
        review_after=due or _due(days=14),
    )
    db.commit()
    return result


def _capture(db, owner: str = "owner", *, sha: str = SHA, capture_id: str = LESSON, due=None):
    result = lesson_reviews.ensure_capture_review(
        db, owner, capture_id=capture_id, capture_hash=sha, artifact_id="artifact",
        title="Review captured hypothesis", acceptance=ACCEPTANCE,
        review_after=due or _due(days=14),
    )
    db.commit()
    return result


def _node(db, node_id: str) -> sqlite3.Row:
    return db.execute("SELECT * FROM plan_nodes WHERE id=?", (node_id,)).fetchone()


def _armed(db, node_id: str) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT condition_type, condition_value, expected_event FROM expected_event_watches
           WHERE plan_node_id=? AND status='armed'""",
        (node_id,),
    ).fetchall()


def _close_reason(db, node_id: str) -> str:
    from plans.closure import settled_reasons

    return settled_reasons(db, [_node(db, node_id)])[node_id]


def _review_nodes(db) -> list[str]:
    return [str(row[0]) for row in db.execute(
        "SELECT DISTINCT node_id FROM plan_node_artifact_links WHERE provenance=?",
        (lesson_reviews.PROVENANCE,),
    )]


def test_the_review_is_pending_linked_and_scheduled_for_its_date(db):
    due = _due(days=14)

    node_id, created = _lesson(db, due=due)

    assert created is True
    assert _node(db, node_id)["state"] == "pending"
    [watch] = _armed(db, node_id)
    assert (watch["condition_type"], watch["expected_event"]) == ("deadline", "scheduled")
    assert datetime.fromisoformat(watch["condition_value"]) == datetime.fromisoformat(due)
    [link] = db.execute("SELECT * FROM plan_node_artifact_links WHERE node_id=?", (node_id,))
    assert (link["artifact_id"], link["role"], link["provenance"], link["source_event_id"]) == (
        "artifact", "input", "lesson.trial_review", f"lesson:{LESSON}:{SHA}",
    )


def test_a_pending_scheduled_review_is_never_selected(db):
    node_id, _created = _lesson(db)
    plan = service.ensure_plan("thread", "owner", db=db)

    reconciled = admission.reconcile_plan(
        plan.id, actor_id=None, provenance="test.lesson_reviews", db=db,
    )
    db.commit()

    assert reconciled.selected == ()
    assert selection_validity.first_eligible_active_id(db, service._plan_row(db, plan.id)) is None
    node = _node(db, node_id)
    assert (node["state"], node["executor_thread_id"]) == ("pending", None)
    assert admission.project(node_id, db=db)["condition"] == "waiting"


def test_closing_other_work_does_not_start_the_review(db):
    node_id, _created = _lesson(db)
    plan = service.ensure_plan("thread", "owner", db=db)
    task = service.add_node(
        plan.id, kind="task", title="Other work", acceptance_criteria="Done", db=db,
    )
    service.transition_node(task.id, "active", db=db)
    db.commit()

    mutate(
        plan.id, "close_node",
        {"node_id": task.id, "reason": "not needed",
         "expected_revision": _node(db, task.id)["revision"]},
        actor=PlanActor("owner", "user", "project"), provenance="test.lesson_reviews",
        mutation_id="close-other", db=db,
    )
    db.commit()

    assert _node(db, node_id)["state"] == "pending"
    assert len(_armed(db, node_id)) == 1


def test_a_retry_finds_the_same_review(db):
    first, _created = _lesson(db)

    again, created = _lesson(db)

    assert (again, created) == (first, False)
    assert _review_nodes(db) == [first]
    assert len(_armed(db, first)) == 1


def test_a_capture_and_a_lesson_with_one_id_and_hash_keep_separate_keys(db):
    capture, _ = _capture(db)

    lesson, created = _lesson(db)

    assert created is True and lesson != capture
    keys = {str(row[0]) for row in db.execute(
        "SELECT source_event_id FROM plan_node_artifact_links"
    )}
    assert keys == {f"capture:{LESSON}:{SHA}", f"lesson:{LESSON}:{SHA}"}


def test_the_trial_review_closes_its_capture_review_as_superseded(db):
    capture, _ = _capture(db)

    lesson, _ = _lesson(db)

    assert _node(db, capture)["state"] == "closed"
    assert _close_reason(db, capture) == lesson_reviews.SUPERSEDED_BY_TRIAL
    assert _armed(db, capture) == []
    assert _node(db, lesson)["state"] == "pending"


def test_appended_evidence_finds_the_capture_review(db):
    first, created = _capture(db)

    again, appended = _capture(db)

    assert (created, appended, again) == (True, False, first)
    assert _review_nodes(db) == [first]


def test_a_changed_hypothesis_replaces_its_capture_review(db):
    first, _ = _capture(db)

    second, created = _capture(db, sha=OTHER_SHA)

    assert created is True and second != first
    assert _close_reason(db, first) == lesson_reviews.SUPERSEDED_BY_CAPTURE
    assert lesson_reviews.open_capture_review_counts(db) == {"owner": 1}


def test_a_behavioral_revision_gets_a_new_review_and_closes_the_prior_one(db):
    first, _ = _lesson(db)

    second, created = _lesson(db, sha=OTHER_SHA)

    assert created is True and second != first
    assert _close_reason(db, first) == lesson_reviews.SUPERSEDED_BY_VERSION
    assert _armed(db, first) == []
    assert len(_armed(db, second)) == 1


def test_a_new_owner_gets_the_review_and_the_old_one_closes(db):
    first, _ = _lesson(db, "owner")

    second, created = _lesson(db, "other")

    assert created is True
    assert _close_reason(db, first) == lesson_reviews.SUPERSEDED_BY_OWNER
    assert _node(db, second)["plan_id"] == service.ensure_plan("thread", "other", db=db).id


@pytest.mark.parametrize("owner", ["nobody", "archived"])
def test_a_missing_or_archived_owner_closes_nothing(db, owner):
    _thread(db, "archived")
    db.execute("UPDATE threads SET lifecycle_state='archived', archived_at='t' WHERE id='archived'")
    capture, _ = _capture(db)
    prior, _ = _lesson(db, sha=OTHER_SHA, lesson_id="11111111-1111-5111-9111-111111111111")

    with pytest.raises(lesson_reviews.LessonOwnerError):
        _lesson(db, owner)
    with pytest.raises(lesson_reviews.LessonOwnerError):
        _capture(db, owner, sha=OTHER_SHA)

    assert _node(db, capture)["state"] == "pending"
    assert _node(db, prior)["state"] == "pending"
    assert sorted(_review_nodes(db)) == sorted([capture, prior])


def test_a_failed_schedule_leaves_no_node_and_closes_nothing(db, monkeypatch):
    capture, _ = _capture(db)

    def fail(*_args, **_kwargs):
        raise RuntimeError("schedule failed")

    monkeypatch.setattr(scheduled_wait, "schedule_task", fail)
    with pytest.raises(RuntimeError, match="schedule failed"), write_transaction(
        db, scope="test_lesson_reviews",
    ) as connection:
        lesson_reviews.ensure_lesson_review(
            connection, "owner", lesson_id=LESSON, behavioral_sha256=SHA,
            artifact_id="artifact", title="Review", acceptance=ACCEPTANCE,
            review_after=_due(days=14),
        )

    assert _review_nodes(db) == [capture]
    assert _node(db, capture)["state"] == "pending"
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 1


def test_the_review_reference_must_be_this_version_open_and_this_owners(db):
    node_id, _ = _lesson(db)

    def refusal(node=node_id, owner="owner", sha=SHA):
        return lesson_reviews.lesson_review_refusal(
            db, node, owner_thread_id=owner, lesson_id=LESSON, behavioral_sha256=sha,
        )

    assert refusal() is None
    assert refusal(node="made-up") == "no review covers this version of the trial"
    assert refusal(sha=OTHER_SHA) == "no review covers this version of the trial"
    assert refusal(owner="other") == "its review belongs to another owner"
    capture, _ = _capture(db, capture_id="22222222-2222-5222-9222-222222222222")
    assert refusal(node=capture) == "no review covers this version of the trial"

    mutate(
        service.ensure_plan("thread", "owner", db=db).id, "close_node",
        {"node_id": node_id, "reason": "retired: no supported reads",
         "expected_revision": _node(db, node_id)["revision"]},
        actor=PlanActor("owner", "user", "project"), provenance="test.lesson_reviews",
        mutation_id="retire", db=db,
    )
    db.commit()

    assert refusal() == "its review is settled"
    assert _armed(db, node_id) == []


def _deliveries():
    seen: list[tuple[str, str, str]] = []

    async def deliver(thread_id, event, summary, _details):
        seen.append((thread_id, event, summary))
        return True

    return seen, deliver


async def _sweep(db, at: str, deliver) -> None:
    await runner.sweep(
        (datetime.fromisoformat(at) + timedelta(minutes=1)).isoformat(), db=db,
        sources=[WatchExpirySource()], deliver=deliver,
    )


@pytest.mark.asyncio
async def test_the_due_review_wakes_its_owner_by_name(db):
    due = _due(days=14, minutes=1)
    node_id, _ = _lesson(db, due=due)
    seen, deliver = _deliveries()

    await _sweep(db, due, deliver)

    assert seen == [("owner", "plan.watch_expired", (
        f'Scheduled task "Review trial lesson: cite-the-head" is due (task {node_id}). '
        "You set it aside 14 days ago: Its review date. "
        "Start it now, schedule it again with a reason, close it, or raise a decision."
    ))]
    assert _node(db, node_id)["state"] == "pending"


@pytest.mark.asyncio
async def test_nothing_wakes_before_the_date(db):
    due = _due(days=14)
    _lesson(db, due=due)
    seen, deliver = _deliveries()

    await _sweep(db, _due(days=13), deliver)

    assert seen == []


def _plan_change(db, operation: str) -> None:
    plan = service.ensure_plan("thread", "owner", db=db)
    mutate(
        plan.id, operation, {"expected_revision": service._plan_row(db, plan.id)["revision"]},
        actor=PlanActor(None, "user", "project", kind="user"),
        provenance="test.lesson_reviews", mutation_id=operation, db=db,
    )
    db.commit()


@pytest.mark.asyncio
async def test_a_paused_owner_is_woken_for_the_due_review_once_resumed(db):
    due = _due(days=14, minutes=1)
    node_id, _ = _lesson(db, due=due)
    _plan_change(db, "pause")
    seen, deliver = _deliveries()

    await _sweep(db, due, deliver)

    assert seen == []
    assert len(_armed(db, node_id)) == 1
    assert _node(db, node_id)["state"] == "pending"

    _plan_change(db, "resume")
    await _sweep(db, due, deliver)

    [(thread, event, summary)] = seen
    assert (thread, event) == ("owner", "plan.watch_expired")
    assert f"is due (task {node_id})" in summary


def _watch_trigger_state(db) -> str | None:
    row = db.execute(
        "SELECT state FROM continuation_triggers WHERE kind='t2_watch_expiry'"
    ).fetchone()
    return row["state"] if row else None


def _detailed_deliveries():
    seen: list[tuple[str, str, str, dict]] = []

    async def deliver(thread_id, event, summary, details):
        seen.append((thread_id, event, summary, details))
        return True

    return seen, deliver


@pytest.mark.asyncio
async def test_a_review_wake_reaches_a_busy_owner_in_the_sweep_it_falls_due(db):
    from automation import plan_liveness

    plan = service.ensure_plan("thread", "owner", db=db)
    task = service.add_node(
        plan.id, kind="task", title="Other work", acceptance_criteria="Done", db=db,
    )
    service.transition_node(task.id, "active", db=db)
    admission.reconcile_plan(plan.id, actor_id=None, provenance="test.lesson_reviews", db=db)
    due = _due(days=14)
    node_id, _ = _lesson(db, due=due)
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES ('turn-1','owner',0,'native/model','running','2000-01-01T00:00:00','boot-1')""",
    )
    db.commit()
    seen, deliver = _detailed_deliveries()
    sources = [LivenessSource(), WatchExpirySource()]

    await runner.sweep(
        (datetime.fromisoformat(due) + timedelta(seconds=1)).isoformat(), db=db,
        sources=sources, deliver=deliver,
    )

    expired = [d for _thread, event, _summary, d in seen if event == "plan.watch_expired"]
    assert len(expired) == 1 and expired[0]["plan_node_id"] == node_id
    assert _watch_trigger_state(db) == "delivered"

    plan_liveness.on_turn_complete("owner", db=db)
    await runner.sweep(
        (datetime.fromisoformat(due) + timedelta(minutes=5)).isoformat(), db=db,
        sources=sources, deliver=deliver,
    )
    assert sum(1 for _t, event, _s, _d in seen if event == "plan.watch_expired") == 1


OWNERS = ("owner", "other", "third")
DATES = 4
PER_DATE = 4
#: Twelve reviews due at one instant.
BURST = len(OWNERS) * PER_DATE


@pytest.mark.asyncio
async def test_owner_and_date_workload_is_one_unselected_review_and_one_wake_each(db):
    """Deterministic stress: 3 owners x 4 review dates x 4 captured hypotheses.

    It pins what this slice costs an owner with no batching: one pending node
    and one wake per captured hypothesis. It is not an arrival rate.
    """
    _thread(db, "third")
    db.commit()
    dues = [_due(days=7 * (index + 1), minutes=1) for index in range(DATES)]
    workload = [
        (owner, f"{owner}-{date_index}-{item}", due)
        for owner in OWNERS for date_index, due in enumerate(dues) for item in range(PER_DATE)
    ]

    created = [_capture(db, owner, capture_id=capture, due=due) for owner, capture, due in workload]

    assert all(made for _node_id, made in created)
    per_owner = DATES * PER_DATE
    assert lesson_reviews.open_capture_review_counts(db) == dict.fromkeys(OWNERS, per_owner)
    for owner in OWNERS:
        plan = service.ensure_plan("thread", owner, db=db)
        reconciled = admission.reconcile_plan(
            plan.id, actor_id=None, provenance="test.lesson_reviews", db=db,
        )
        assert reconciled.selected == ()
    db.commit()
    assert {row[0] for row in db.execute("SELECT state FROM plan_nodes")} == {"pending"}

    retried = [_capture(db, owner, capture_id=capture, due=due) for owner, capture, due in workload]
    assert [node_id for node_id, _ in retried] == [node_id for node_id, _ in created]
    assert not any(made for _node_id, made in retried)
    assert len(_review_nodes(db)) == len(workload)

    seen, deliver = _deliveries()
    for minute in range(1, 121):
        await runner.sweep(
            (datetime.fromisoformat(dues[0]) + timedelta(minutes=minute)).isoformat(), db=db,
            sources=[WatchExpirySource()], deliver=deliver,
        )
    woken = [thread for thread, _event, _summary in seen]
    assert {owner: woken.count(owner) for owner in OWNERS} == dict.fromkeys(OWNERS, PER_DATE)
    assert len({summary for _thread, _event, summary in seen}) == BURST

    for index in range(PER_DATE):
        lesson_reviews.ensure_lesson_review(
            db, "owner", lesson_id=f"owner-0-{index}", behavioral_sha256=SHA,
            artifact_id="artifact", title="Review trial lesson", acceptance=ACCEPTANCE,
            review_after=_due(days=30),
        )
    db.commit()
    assert lesson_reviews.open_capture_review_counts(db) == {
        "owner": per_owner - PER_DATE, "other": per_owner, "third": per_owner,
    }


# -- a settled review is never the current one -----------------------------------


def _open_reviews(db, key_prefix: str) -> list[str]:
    return [str(row["node_id"]) for row in lesson_reviews._open_reviews(db, key_prefix)]


def _lesson_refusal(db, node_id: str, owner: str = "owner", sha: str = SHA) -> str | None:
    return lesson_reviews.lesson_review_refusal(
        db, node_id, owner_thread_id=owner, lesson_id=LESSON, behavioral_sha256=sha,
    )


def _owner_closes(db, node_id: str, owner: str = "owner") -> None:
    mutate(
        service.ensure_plan("thread", owner, db=db).id, "close_node",
        {"node_id": node_id, "reason": "retired: no supported reads",
         "expected_revision": _node(db, node_id)["revision"]},
        actor=PlanActor(owner, "user", "project"), provenance="test.lesson_reviews",
        mutation_id=f"close-{node_id}", db=db,
    )
    db.commit()


def test_a_version_that_comes_back_gets_a_new_review(db):
    first, _ = _lesson(db)
    second, _ = _lesson(db, sha=OTHER_SHA)

    third, created = _lesson(db)

    assert created is True and third not in (first, second)
    assert _open_reviews(db, f"lesson:{LESSON}:") == [third]
    assert _lesson_refusal(db, third) is None
    assert _lesson_refusal(db, first) == "its review is settled"
    assert _close_reason(db, second) == lesson_reviews.SUPERSEDED_BY_VERSION
    assert len(_armed(db, third)) == 1 and _armed(db, first) == [] and _armed(db, second) == []
    # Two nodes carry one key: the settled review and the open one.
    assert sorted(str(row[0]) for row in db.execute(
        "SELECT node_id FROM plan_node_artifact_links WHERE source_event_id=?",
        (lesson_reviews.lesson_key(LESSON, SHA),),
    )) == sorted([first, third])


def test_an_owner_who_comes_back_gets_a_new_review(db):
    first, _ = _lesson(db, "owner")
    second, _ = _lesson(db, "other")

    third, created = _lesson(db, "owner")

    assert created is True and third not in (first, second)
    assert _open_reviews(db, f"lesson:{LESSON}:") == [third]
    assert _lesson_refusal(db, third) is None
    assert _close_reason(db, second) == lesson_reviews.SUPERSEDED_BY_OWNER
    assert _node(db, third)["plan_id"] == service.ensure_plan("thread", "owner", db=db).id


def test_a_version_placed_again_after_its_review_was_settled_gets_a_new_review(db):
    first, _ = _lesson(db)
    _owner_closes(db, first)

    second, created = _lesson(db)

    assert created is True and second != first
    assert _open_reviews(db, f"lesson:{LESSON}:") == [second]
    assert _lesson_refusal(db, second) is None
    assert len(_armed(db, second)) == 1

    again, repeated = _lesson(db)

    assert (again, repeated) == (second, False)


def test_an_archived_review_is_not_found_again(db):
    first, _ = _lesson(db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (first,))
    db.commit()

    second, created = _lesson(db)

    assert created is True and second != first
    assert _open_reviews(db, f"lesson:{LESSON}:") == [second]


def test_a_captured_hypothesis_that_comes_back_gets_a_new_review(db):
    first, _ = _capture(db)
    second, _ = _capture(db, sha=OTHER_SHA)

    third, created = _capture(db)

    assert created is True and third not in (first, second)
    assert _open_reviews(db, f"capture:{LESSON}:") == [third]
    assert _close_reason(db, second) == lesson_reviews.SUPERSEDED_BY_CAPTURE

    _owner_closes(db, third)
    fourth, created = _capture(db)

    assert created is True and fourth != third
    assert _open_reviews(db, f"capture:{LESSON}:") == [fourth]


# -- the wake follows the file's review date --------------------------------------


def _waits(db, node_id: str) -> list[tuple[str, str]]:
    """Every scheduled wait the review ever held, oldest first, as (status, date)."""
    return [(str(row["status"]), str(row["condition_value"])) for row in db.execute(
        """SELECT status, condition_value FROM expected_event_watches
           WHERE plan_node_id=? ORDER BY armed_at, rowid""",
        (node_id,),
    )]


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.asyncio
@pytest.mark.parametrize("moved", [{"days": 30}, {"days": 7}], ids=["later", "earlier"])
async def test_the_same_version_with_another_date_moves_its_one_wake(db, moved):
    due, other = _due(days=14, minutes=1), _due(**moved, minutes=1)
    first, _ = _lesson(db, due=due)

    node_id, created = _lesson(db, due=other)

    assert (node_id, created) == (first, False)
    [(old_status, old_date), (new_status, new_date)] = _waits(db, node_id)
    assert (old_status, new_status) == ("resolved", "armed")
    assert (_instant(old_date), _instant(new_date)) == (_instant(due), _instant(other))
    assert _lesson_refusal(db, node_id) is None
    seen, deliver = _deliveries()
    earlier, later = sorted([due, other], key=_instant)

    await _sweep(db, earlier, deliver)

    assert len(seen) == (1 if other == earlier else 0)

    await _sweep(db, later, deliver)

    assert len(seen) == 1 and f"is due (task {node_id})" in seen[0][2]
    assert _armed(db, node_id) == []


def test_the_same_version_with_the_same_date_arms_nothing_more(db):
    due = _due(days=14)
    first, _ = _lesson(db, due=due)
    events = db.execute("SELECT COUNT(*) FROM plan_node_events").fetchone()[0]

    node_id, created = _lesson(db, due=due.replace("+00:00", "Z"))

    assert (node_id, created) == (first, False)
    assert [status for status, _date in _waits(db, node_id)] == ["armed"]
    assert db.execute("SELECT COUNT(*) FROM plan_node_events").fetchone()[0] == events


def test_a_date_that_goes_back_to_the_first_one_keeps_one_wake(db):
    due, other = _due(days=14), _due(days=30)
    node_id, _ = _lesson(db, due=due)
    _lesson(db, due=other)

    _lesson(db, due=due)

    assert sorted(status for status, _date in _waits(db, node_id)) == [
        "armed", "resolved", "resolved",
    ]
    [armed] = _armed(db, node_id)
    assert _instant(armed["condition_value"]) == _instant(due)


def test_a_captured_hypothesis_with_another_date_moves_its_one_wake(db):
    node_id, _ = _capture(db, due=_due(days=14))
    other = _due(days=21)

    again, created = _capture(db, due=other)

    assert (again, created) == (node_id, False)
    [armed] = _armed(db, node_id)
    assert _instant(armed["condition_value"]) == _instant(other)


@pytest.mark.asyncio
async def test_a_paused_owners_review_moves_its_wake_and_wakes_once_resumed(db):
    due, other = _due(days=14, minutes=1), _due(days=30, minutes=1)
    node_id, _ = _lesson(db, due=due)
    _plan_change(db, "pause")

    again, created = _lesson(db, due=other)

    assert (again, created) == (node_id, False)
    [armed] = _armed(db, node_id)
    assert _instant(armed["condition_value"]) == _instant(other)
    seen, deliver = _deliveries()

    await _sweep(db, other, deliver)

    assert seen == [] and len(_armed(db, node_id)) == 1

    _plan_change(db, "resume")
    await _sweep(db, other, deliver)

    assert len(seen) == 1 and f"is due (task {node_id})" in seen[0][2]


@pytest.mark.asyncio
async def test_a_fired_review_is_not_armed_again_for_the_date_that_already_fired(db):
    due = _due(days=14)
    node_id, _ = _lesson(db, due=due)
    _seen, deliver = _detailed_deliveries()
    await _sweep(db, due, deliver)

    again, created = _lesson(db, due=due)

    assert (again, created) == (node_id, False)
    assert _armed(db, node_id) == []
    assert [status for status, _date in _waits(db, node_id)] == ["expired"]
    assert node_id in liveness_signal("owner", db=db).node_ids


@pytest.mark.asyncio
async def test_a_fired_review_given_a_later_date_is_scheduled_for_it(db):
    due = _due(days=14)
    node_id, _ = _lesson(db, due=due)
    _seen, deliver = _detailed_deliveries()
    await _sweep(db, due, deliver)
    other = _due(days=30)

    again, created = _lesson(db, due=other)

    assert (again, created) == (node_id, False)
    assert [status for status, _date in _waits(db, node_id)] == ["expired", "armed"]
    [armed] = _armed(db, node_id)
    assert _instant(armed["condition_value"]) == _instant(other)
    assert _node(db, node_id)["state"] == "pending"


def test_the_owners_own_wake_for_the_new_date_is_kept_and_the_stale_one_resolved(db):
    due, other = _due(days=14), _due(days=30)
    node_id, _ = _lesson(db, due=due)
    mutate(
        service.ensure_plan("thread", "owner", db=db).id, "arm_watch",
        {"node_id": node_id, "expected_revision": _node(db, node_id)["revision"],
         "condition": {"type": "deadline", "value": other}, "reason": "extended"},
        actor=PlanActor("owner", "user", "project"), provenance="test.lesson_reviews",
        mutation_id="extend", db=db,
    )
    db.commit()
    # A task has one schedule (#903): the owner's wait replaces the review's own wake.
    [owners] = _armed(db, node_id)
    assert _instant(owners["condition_value"]) == _instant(other)

    again, created = _lesson(db, due=other)

    assert (again, created) == (node_id, False)
    [armed] = _armed(db, node_id)
    assert _instant(armed["condition_value"]) == _instant(other)
    assert sorted(status for status, _date in _waits(db, node_id)) == ["armed", "resolved"]


# -- a review its owner is working is the owner's ---------------------------------


def _owner_does(db, operation: str, node_id: str, **params) -> None:
    mutate(
        service.ensure_plan("thread", "owner", db=db).id, operation,
        {"node_id": node_id, "expected_revision": _node(db, node_id)["revision"], **params},
        actor=PlanActor("owner", "user", "project"), provenance="test.lesson_reviews",
        mutation_id=f"{operation}-{node_id}", db=db,
    )
    db.commit()


def _owner_starts(db, node_id: str) -> None:
    watch = db.execute(
        "SELECT id FROM expected_event_watches WHERE plan_node_id=? AND status='armed'",
        (node_id,),
    ).fetchone()["id"]
    _owner_does(db, "resolve_watch", node_id, watch_id=watch)
    _owner_does(db, "start", node_id)


def _attempts(db, node_id: str) -> list[tuple]:
    return [tuple(row) for row in db.execute(
        "SELECT * FROM plan_node_attempts WHERE node_id=? ORDER BY selected_at, id", (node_id,),
    )]


def _untouched(db, node_id: str) -> tuple:
    node = _node(db, node_id)
    events = db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE node_id=?", (node_id,),
    ).fetchone()[0]
    return (node["state"], node["revision"], _waits(db, node_id), _attempts(db, node_id), events)


def test_a_review_its_owner_started_keeps_its_waits_and_attempt_when_the_date_moves(db):
    node_id, _ = _lesson(db, due=_due(days=14))
    _owner_starts(db, node_id)
    before = _untouched(db, node_id)
    assert before[0] == "active" and _armed(db, node_id) == []
    assert admission_attempts.open_attempt(db, node_id) is not None

    again, created = _lesson(db, due=_due(days=30))

    assert (again, created) == (node_id, False)
    assert _untouched(db, node_id) == before
    assert _lesson_refusal(db, node_id) is None
    assert _open_reviews(db, f"lesson:{LESSON}:") == [node_id]


def test_a_review_handed_for_review_keeps_its_waits_when_the_date_moves(db):
    node_id, _ = _lesson(db, due=_due(days=14))
    _owner_starts(db, node_id)
    _owner_does(db, "request_review", node_id, evidence="Three reads, one supported.")
    before = _untouched(db, node_id)
    assert before[0] == "review_requested"

    again, created = _lesson(db, due=_due(days=30))

    assert (again, created) == (node_id, False)
    assert _untouched(db, node_id) == before
    assert _armed(db, node_id) == []


def test_a_captured_hypothesis_its_owner_started_keeps_its_waits_when_the_date_moves(db):
    node_id, _ = _capture(db, due=_due(days=14))
    _owner_starts(db, node_id)
    before = _untouched(db, node_id)

    again, created = _capture(db, due=_due(days=30))

    assert (again, created) == (node_id, False)
    assert _untouched(db, node_id) == before


def test_an_open_attempt_alone_keeps_the_waits_when_the_date_moves(db):
    """The guard does not rest on the node's state: an open attempt is enough."""
    node_id, _ = _lesson(db, due=_due(days=14))
    _owner_starts(db, node_id)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='pending' WHERE id=?", (node_id,))
    db.commit()
    before = _untouched(db, node_id)
    assert before[0] == "pending" and admission_attempts.open_attempt(db, node_id) is not None

    _lesson(db, due=_due(days=30))

    assert _untouched(db, node_id) == before


@pytest.mark.asyncio
async def test_a_pending_review_given_a_past_date_wakes_once_and_keeps_no_dead_wait(db):
    node_id, _ = _lesson(db, due=_due(days=14))
    past = _due(days=-1)

    again, created = _lesson(db, due=past)

    assert (again, created) == (node_id, False)
    assert [status for status, _date in _waits(db, node_id)] == ["resolved", "armed"]
    seen, deliver = _deliveries()

    await _sweep(db, _due(), deliver)

    assert len(seen) == 1 and f"is due (task {node_id})" in seen[0][2]
    assert _armed(db, node_id) == []

    _lesson(db, due=past)
    await _sweep(db, _due(minutes=5), deliver)

    assert len(seen) == 1 and _armed(db, node_id) == []
    assert [status for status, _date in _waits(db, node_id)] == ["resolved", "expired"]
    assert _node(db, node_id)["state"] == "pending"
