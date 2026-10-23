"""A hand-over carries the task itself when its executor did not write it (DCOL-FR4.3b).

A thread handed a task somebody else wrote has not read it. Both ways a task
reaches its executor say what it is: the work-selected wake, and the result of
the close that auto-selects it. Each carries `selected_description` beside the
`next` block: the task's id, its description, whole up to a ceiling and past
it cut with the read that returns the rest, and whether it was cut. A task its
executor wrote, or one whose author is unknown, carries nothing, and the key is
absent. Every hand-over carries it, a task set aside and taken back included:
nothing records what was carried.

The ceiling and the block cap are restated here rather than imported, so a
production constant that drifts fails a test instead of moving the assertion.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import feedback_batches
from infra.database import get_db, write_transaction
from plans import admission, handoff_brief, review_requests, service
from plans.boundary import PlanActor, mutate
from plans.feedback_nodes import AGENT_FRAMING, PROVENANCE
from tests.plan_close_next_helpers import (
    HUMAN,
    NEXT_BLOCK_CAP,
    OWNER,
    EventCapture,
    close,
    close_params,
    open_attempt,
    owner_plan,
    pause,
    plan_update,
    queue_active,
    request_review,
    revision,
    row,
    select,
    serialized,
    size,
    task,
)
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture
from tests.plan_writer_authorizer import raw_plan_seed

KEY = "selected_description"
#: Serialized-byte ceiling of one carried description, as the tool emits it.
BRIEF_CAP = 16384
CRITERIA = (
    "Every item is acknowledged, resolved, or replied to, and every change made for one is also made "
    "wherever this document says the same thing in other words."
)
SMALL = "Comment c-1\nQuote: quoted source\nFeedback: Please check this point."
#: Over the ceiling on its own, so the cut is the description's, not the block's.
LARGE = "Feedback: " + "check this point against the source. " * 600


def _cut_marker(node_id: str) -> str:
    return f"… [cut to fit; plan_read(node_id='{node_id}') returns its full description]"


def _whole(node_id: str, text: str) -> dict:
    """The fact a hand-over carries for a description that fits its ceiling."""
    return {"node_id": node_id, "text": text, "cut": False}


def _handed(
    db, plan_id: str, description: str | None, *, author: str | None, known: bool = True,
):
    """Add one active task written by ``author``, as a trusted writer queues it.

    ``known=False`` hands over a task whose author the event log never recorded.
    """
    node = service.add_node(
        plan_id, kind="task", title="Reviewer left 3 comments", acceptance_criteria=CRITERIA,
        description=description, actor_id=author, provenance="test.handoff", db=db,
    )
    if not known:
        node = _without_creation_event(db, node.id)
    service.transition_node(node.id, "active", actor_id=author, provenance="test.handoff", db=db)
    db.commit()
    return node


def _without_creation_event(db, node_id: str):
    """Return a task with no ``node_created`` event, as a row older than the log is.

    The event log is append-only, so the event cannot be removed: the row is
    copied from the real task instead, and the copy never had one.
    """
    row = dict(service._node_row(db, node_id), id=uuid.uuid4().hex)
    row["order_key"] += 1
    marks = ",".join("?" for _ in row)
    with raw_plan_seed(db):
        db.execute(f"INSERT INTO plan_nodes ({','.join(row)}) VALUES ({marks})", tuple(row.values()))
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE node_id=?", (row["id"],),
    ).fetchone()[0] == 0
    return service.get_node(row["id"], db=db)


def _wakes(db, thread_id: str = "owner") -> list[tuple[str, dict]]:
    """Return the id and facts of each work-selected wake a thread received, in order."""
    rows = db.execute(
        "SELECT id,content FROM messages WHERE thread_id=? "
        "AND id LIKE 'plan-wake:work_selected:%' ORDER BY input_order",
        (thread_id,),
    ).fetchall()
    return [(row["id"], json.loads(row["content"].split("\n", 1)[1])) for row in rows]


def _wake(db, thread_id: str = "owner") -> tuple[str, dict]:
    """Return the id and facts of the one work-selected wake a thread received."""
    wakes = _wakes(db, thread_id)
    assert len(wakes) == 1, [wake_id for wake_id, _facts in wakes]
    line = db.execute("SELECT content FROM messages WHERE id=?", (wakes[0][0],)).fetchone()[0]
    assert line.split("\n", 1)[0] == (
        "[CORSIA plan] Reviewer left 3 comments is now your current task."
    )
    return wakes[0]


def _reconciled_by_another(db, plan_id: str):
    """Reconcile a plan in a write scope of its own, as every production writer does."""
    with write_transaction(db, scope="test_handoff"):
        return admission.reconcile_plan(
            plan_id, actor_id=None, provenance="test.handoff", db=db,
        )


def _selected_by_another(db, description: str | None, *, author: str | None, known: bool = True):
    """Hand a free executor one task through a write that is not its own."""
    plan = owner_plan(db)
    node = _handed(db, plan.id, description, author=author, known=known)
    _reconciled_by_another(db, plan.id)
    return node, _wake(db)


def _closed_into(db, description: str | None, *, author: str | None, known: bool = True):
    """Close the executor's current task so the queued task takes its slot."""
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    select(db, plan.id, current.id)
    node = _handed(db, plan.id, description, author=author, known=known)
    result = close(db, plan.id, "mark_done", current.id)
    assert (result["next"]["selected"]["id"], result["next"]["selected"]["how"]) == (
        node.id, "auto_selected",
    )
    return node, result


def test_the_wake_carries_a_task_somebody_else_wrote_whole(db):
    node, (_wake_id, facts) = _selected_by_another(db, SMALL, author="reviewer")

    assert facts[KEY] == _whole(node.id, SMALL)
    assert facts["next"]["selected"]["id"] == node.id
    assert KEY not in facts["next"]


def test_the_wake_cuts_a_description_past_the_ceiling_and_names_the_read(db):
    node, (_wake_id, facts) = _selected_by_another(db, LARGE, author="reviewer")

    text = facts[KEY]["text"]
    marker = _cut_marker(node.id)
    assert facts[KEY]["node_id"] == node.id and facts[KEY]["cut"] is True
    assert text.endswith(marker) and LARGE.startswith(text[: -len(marker)])
    assert size(text) <= BRIEF_CAP < size(LARGE)
    # Longest prefix: the cut spends the ceiling rather than stopping short.
    assert size(LARGE[: len(text) - len(marker) + 1] + marker) > BRIEF_CAP


def test_the_wake_carries_nothing_for_a_task_its_executor_wrote(db):
    _node, (_wake_id, facts) = _selected_by_another(db, SMALL, author="owner")

    assert KEY not in facts and SMALL not in json.dumps(facts)


def test_the_wake_carries_nothing_when_the_author_is_unknown(db):
    _node, (_wake_id, facts) = _selected_by_another(
        db, SMALL, author="reviewer", known=False,
    )

    assert KEY not in facts and SMALL not in json.dumps(facts)


def test_the_wake_carries_nothing_for_a_task_with_no_description(db):
    _node, (_wake_id, facts) = _selected_by_another(db, None, author="reviewer")

    assert KEY not in facts


@pytest.mark.parametrize("author", ["reviewer", "owner"])
def test_the_wake_is_the_same_one_wake_with_and_without_the_description(db, author):
    """The description rides in the wake's text; its identity and count do not move."""
    node, (wake_id, facts) = _selected_by_another(db, LARGE, author=author)

    assert (KEY in facts) == (author == "reviewer")
    assert wake_id == f"plan-wake:work_selected:{node.id}:{revision(db, node.id)}"
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='owner' AND id LIKE 'plan-wake:%'"
    ).fetchone()[0] == 1


def test_the_close_result_carries_a_task_somebody_else_wrote_beside_next(db):
    node, result = _closed_into(db, SMALL, author="reviewer")

    assert result[KEY] == _whole(node.id, SMALL)
    assert result[KEY]["node_id"] == result["next"]["selected"]["id"]
    assert KEY not in result["next"] and SMALL not in json.dumps(result["next"])


def test_the_close_result_cuts_a_description_past_the_ceiling_and_names_the_read(db):
    node, result = _closed_into(db, LARGE, author="reviewer")

    text = result[KEY]["text"]
    marker = _cut_marker(node.id)
    assert result[KEY]["cut"] is True
    assert text.endswith(marker) and LARGE.startswith(text[: -len(marker)])
    assert size(text) <= BRIEF_CAP < size(LARGE)
    assert size(LARGE[: len(text) - len(marker) + 1] + marker) > BRIEF_CAP


def test_the_close_result_carries_nothing_for_a_task_its_executor_wrote(db):
    _node, result = _closed_into(db, SMALL, author="owner")

    assert KEY not in result


def test_the_close_result_carries_nothing_when_the_author_is_unknown(db):
    _node, result = _closed_into(db, SMALL, author="reviewer", known=False)

    assert KEY not in result


def _closed_by_the_user(db, operation: str, *, named: bool):
    """Let the user close the executor's task, filling the executor's slot from the queue.

    The executor has put its task up for review, so its slot is empty and the
    close that settles the review is what hands it the queued task. Only a
    thread plan's owner and the user may write to it, so the user is the one
    closer that is not the executor.
    """
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    select(db, plan.id, current.id)
    request_review(db, plan.id, current.id)
    node = _handed(db, plan.id, SMALL, author="reviewer")
    assert open_attempt(db, node.id) is None
    extra = {"next_node_id": node.id} if named else {}
    result = close(db, plan.id, operation, current.id, actor=HUMAN, **extra)
    assert (result["next"]["selected"]["id"], result["next"]["selected"]["how"]) == (
        node.id, "started" if named else "auto_selected",
    )
    return node, result


@pytest.mark.parametrize(
    ("operation", "named"),
    [("accept", False), ("accept", True), ("close_node", False)],
    ids=["a-reviewers-accept", "an-accept-naming-the-next-task", "a-close"],
)
def test_a_close_by_someone_other_than_the_executor_is_not_handed_its_description(
    db, monkeypatch, operation, named,
):
    """The result returns to the closer, which does not hold the task it selected.

    A slot reconciliation fills still tells the executor: the same write sends
    it the work-selected wake, and the description rides there. A task the
    closer named is started before reconciliation and sends no such wake, so
    only the unnamed cases have one to read.
    """
    capture = EventCapture(db).install(monkeypatch)

    node, result = _closed_by_the_user(db, operation, named=named)

    assert result["next"]["executor_thread_id"] == "owner" and HUMAN.thread_id is None
    assert KEY not in result and SMALL not in json.dumps(result)
    if not named:
        _wake_id, facts = _wake(db)
        assert facts[KEY] == _whole(node.id, SMALL)
    event = capture.close_events()[-1]
    assert event["properties"]["executor_is_actor"] is False
    assert event["properties"]["description_carried"] is False


def test_a_task_already_running_before_the_close_is_not_described_again(db):
    """Only a hand-over describes the task: its holder was told when it took the slot."""
    plan = owner_plan(db)
    reviewed = task(db, plan.id, "Task A")
    select(db, plan.id, reviewed.id)
    handed = _handed(db, plan.id, SMALL, author="reviewer")
    told = request_review(db, plan.id, reviewed.id)
    assert told["next"]["selected"]["how"] == "auto_selected"
    assert told[KEY] == _whole(handed.id, SMALL)

    result = close(db, plan.id, "accept", reviewed.id)

    assert (result["next"]["selected"]["id"], result["next"]["selected"]["how"]) == (
        handed.id, "running",
    )
    assert KEY not in result


def _park(db, plan_id: str, node_id: str, **extra) -> tuple[dict, str]:
    """Set one task aside on a watch as its executor; return the result and the watch id."""
    deadline = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    result = plan_update(db, plan_id, "arm_watch", {
        "node_id": node_id, "expected_revision": revision(db, node_id),
        "condition": {"type": "deadline", "value": deadline},
        "reason": "Waiting on the vendor", **extra,
    })
    return result, result["watch"]["id"]


def _release(db, plan_id: str, node_id: str, watch_id: str, *, actor) -> dict:
    """End the wait a parked task is set aside on."""
    return plan_update(db, plan_id, "resolve_watch", {
        "node_id": node_id, "watch_id": watch_id, "expected_revision": revision(db, node_id),
    }, actor=actor)


def _taken_back_by_wake(db) -> tuple[str, dict]:
    """Hand a task over, take it out of its executor's slot, and let another's write return it.

    The user pauses and resumes it: an agent's own watch is the agent's to end,
    and its own write wakes nobody. Returns the task's id and the facts of the
    wake that returned it.
    """
    node, (_first, facts) = _selected_by_another(db, SMALL, author="reviewer")
    assert facts[KEY]["text"] == SMALL
    plan = owner_plan(db)
    pause(db, plan.id, node.id, actor=HUMAN)
    assert open_attempt(db, node.id) is None
    hold = db.execute(
        "SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (node.id,),
    ).fetchone()
    plan_update(db, plan.id, "resume_node", {
        "node_id": node.id, "hold_id": hold["id"], "expected_revision": revision(db, node.id),
    }, actor=HUMAN)
    wakes = _wakes(db)
    assert len(wakes) == 2, [wake_id for wake_id, _facts in wakes]
    assert wakes[1][1]["next"]["selected"]["id"] == node.id
    return node.id, wakes[1][1]


def _taken_back_in_a_close(db) -> tuple[str, dict]:
    """Hand a task over in a close, park it, and take it back in a later close.

    Returns the task's id and the result of the close that returned it.
    """
    node, result = _closed_into(db, SMALL, author="reviewer")
    assert result[KEY]["text"] == SMALL
    plan = owner_plan(db)
    other = task(db, plan.id, "Other work")
    parked, watch_id = _park(db, plan.id, node.id, next_node_id=other.id)
    assert parked["next"]["selected"]["id"] == other.id
    _release(db, plan.id, node.id, watch_id, actor=OWNER)
    returned = close(db, plan.id, "mark_done", other.id)
    assert (returned["next"]["selected"]["id"], returned["next"]["selected"]["how"]) == (
        node.id, "auto_selected",
    )
    return node.id, returned


def test_a_wake_that_returns_a_parked_task_carries_its_description_again(db):
    """Nothing records what was carried, so no hand-over can assume it was read."""
    node_id, facts = _taken_back_by_wake(db)

    assert facts[KEY]["text"] == SMALL and facts[KEY]["node_id"] == node_id


def test_a_close_that_returns_a_parked_task_carries_its_description_again(db):
    node_id, result = _taken_back_in_a_close(db)

    assert result[KEY]["text"] == SMALL and result[KEY]["node_id"] == node_id


def test_a_note_appended_to_a_parked_review_task_is_carried_when_it_returns(db):
    node, result = _closed_into(db, SMALL, author="reviewer")
    plan = owner_plan(db)
    other = task(db, plan.id, "Other work")
    _parked, watch_id = _park(db, plan.id, node.id, next_node_id=other.id)
    review_requests.append_review_note(db, node.id, {
        "id": "request-2", "requester_display": "Reviewer",
        "created_at": "2026-10-02T15:00:00+00:00", "note": "Check section two as well.",
    })
    _release(db, plan.id, node.id, watch_id, actor=OWNER)

    returned = close(db, plan.id, "mark_done", other.id)

    assert returned[KEY]["node_id"] == node.id
    assert returned[KEY]["text"].startswith(SMALL)
    assert returned[KEY]["text"].endswith("Check section two as well.")


def test_the_next_block_keeps_its_cap_beside_a_description_at_the_ceiling(db):
    """The description is bounded on its own: it takes no byte from the block."""
    plan = owner_plan(db)
    current = task(db, plan.id, "Current work")
    select(db, plan.id, current.id)
    node = service.add_node(
        plan.id, kind="task", title="T" * 60, acceptance_criteria="criteria " * 400,
        description=LARGE, actor_id="reviewer", provenance="test.handoff", db=db,
    )
    service.transition_node(node.id, "active", actor_id="reviewer", provenance="test.handoff", db=db)
    for index in range(4):
        queue_active(db, task(db, plan.id, f"Open task {index} " + "x" * 40).id)

    result = close(db, plan.id, "mark_done", current.id)

    assert result["next"]["selected"]["id"] == node.id
    assert size(result["next"]) <= NEXT_BLOCK_CAP
    assert BRIEF_CAP - 8 <= size(result[KEY]["text"]) <= BRIEF_CAP
    assert len(result["next"]["open_tasks"]) == 3


def test_the_wakes_next_block_keeps_its_cap_beside_a_description_at_the_ceiling(db):
    plan = owner_plan(db)
    node = service.add_node(
        plan.id, kind="task", title="T" * 60, acceptance_criteria="criteria " * 400,
        description=LARGE, actor_id="reviewer", provenance="test.handoff", db=db,
    )
    for index in range(4):
        task(db, plan.id, f"Open task {index} " + "x" * 40)
    service.transition_node(node.id, "active", actor_id="reviewer", provenance="test.handoff", db=db)
    admission.reconcile_plan(plan.id, actor_id=None, provenance="test.handoff", db=db)
    db.commit()

    facts = json.loads(db.execute(
        "SELECT content FROM messages WHERE id LIKE 'plan-wake:work_selected:%'"
    ).fetchone()["content"].split("\n", 1)[1])

    assert facts["next"]["selected"]["id"] == facts[KEY]["node_id"] == node.id
    assert size(facts["next"]) <= NEXT_BLOCK_CAP
    assert BRIEF_CAP - 8 <= size(facts[KEY]["text"]) <= BRIEF_CAP


def _feedback_node(db, owner: str) -> dict:
    """Send one agent comment on ``owner``'s document through the real batch route."""
    artifact_id = seed_document(db, owner, name=f"{owner}.md")
    add_comment(db, artifact_id)
    batch = [row for row in batches(db, state="open") if row["artifact_id"] == artifact_id][0]
    feedback_batches.close_batch(db, batch["id"], "quiet")
    return dict(db.execute(
        """SELECT n.* FROM plan_nodes n
           JOIN plan_node_events e ON e.node_id=n.id
           JOIN plans p ON p.id=n.plan_id
           WHERE e.provenance=? AND e.event='node_created' AND p.owner_id=?""",
        (PROVENANCE, owner),
    ).fetchone())


def test_a_feedback_task_reaches_an_idle_owner_with_its_items_and_framing(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "idle-owner")
        seed_thread(db, "author", title="Reviewer")

        node = _feedback_node(db, "idle-owner")

        _wake_id, facts = _feedback_wake(db, "idle-owner")
        assert facts[KEY] == _whole(node["id"], node["description"])
        assert facts[KEY]["text"].startswith(AGENT_FRAMING)
        assert "Quote: quoted source" in facts[KEY]["text"]
        assert "Feedback: Please check this point." in facts[KEY]["text"]
    finally:
        db.close()


def test_a_feedback_task_reaches_a_busy_owner_in_the_close_that_selects_it(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "busy-owner")
        seed_thread(db, "author", title="Reviewer")
        plan = service.ensure_plan("thread", "busy-owner", db=db)
        current = service.add_node(
            plan.id, kind="task", title="Current work",
            acceptance_criteria="Current work is complete.", db=db,
        )
        service.transition_node(current.id, "active", db=db)
        admission.reconcile_plan(plan.id, actor_id=None, provenance="test", db=db)
        db.commit()

        node = _feedback_node(db, "busy-owner")
        # A busy owner is not preempted, so nothing has told it about the task yet.
        assert node["id"] not in {
            row["id"].split(":")[2] for row in db.execute(
                "SELECT id FROM messages WHERE id LIKE 'plan-wake:work_selected:%'"
            ).fetchall()
        }

        result = mutate(
            plan.id, "mark_done",
            {"node_id": current.id, "evidence": "Focused proof passed",
             "expected_revision": service._node_row(db, current.id)["revision"]},
            actor=PlanActor("busy-owner", "owner-token", None),
            provenance="test.handoff", mutation_id="close-current", db=db,
        ).result

        assert result["next"]["selected"]["id"] == node["id"]
        assert result[KEY] == _whole(node["id"], node["description"])
        assert result[KEY]["text"].startswith(AGENT_FRAMING)
        assert "Feedback: Please check this point." in result[KEY]["text"]
    finally:
        db.close()


def test_feedback_sent_after_a_batch_was_delivered_arrives_as_its_own_task(isolated_db):
    """A delivered batch's task is never rewritten, so a later batch is a first hand-over.

    A parked feedback task therefore gains nothing while it waits: more feedback
    on the same document is a new task, carried whole the first time it is handed
    over like any other.
    """
    db = get_db()
    try:
        seed_thread(db, "idle-owner")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "idle-owner", name="idle-owner.md")

        def deliver() -> list[dict]:
            add_comment(db, artifact_id)
            [batch] = [row for row in batches(db, state="open") if row["artifact_id"] == artifact_id]
            feedback_batches.close_batch(db, batch["id"], "quiet")
            return [dict(row) for row in db.execute(
                """SELECT n.id,n.description FROM plan_nodes n
                   JOIN plan_node_events e ON e.node_id=n.id
                   WHERE e.provenance=? AND e.event='node_created' ORDER BY n.order_key""",
                (PROVENANCE,),
            ).fetchall()]

        [first] = deliver()
        again = deliver()

        assert [row["id"] for row in again][0] == first["id"] and len(again) == 2
        assert again[0]["description"] == first["description"]
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_events WHERE node_id=? "
            "AND event IN ('node_restated','review_request_note_appended')",
            (first["id"],),
        ).fetchone()[0] == 0
    finally:
        db.close()


def _feedback_wake(db, thread_id: str) -> tuple[str, dict]:
    rows = db.execute(
        "SELECT id,content FROM messages WHERE thread_id=? "
        "AND id LIKE 'plan-wake:work_selected:%'",
        (thread_id,),
    ).fetchall()
    assert len(rows) == 1, [row["id"] for row in rows]
    return rows[0]["id"], json.loads(rows[0]["content"].split("\n", 1)[1])


# --- what a hand-over carried is measured, never copied ---------------------

CARRIED = ("description_carried", "description_bytes", "description_cut")


def _carried(event: dict) -> tuple:
    return tuple(event["properties"][key] for key in CARRIED)


def test_the_close_event_measures_a_description_carried_whole(db, monkeypatch):
    capture = EventCapture(db).install(monkeypatch)

    _node, result = _closed_into(db, SMALL, author="reviewer")

    event = capture.close_events()[-1]
    assert _carried(event) == (True, size(result[KEY]["text"]), result[KEY]["cut"])
    assert _carried(event) == (True, size(SMALL), False)
    assert event["in_transaction"] is False
    assert SMALL not in serialized(event["properties"])
    assert "Quote" not in serialized(event["properties"])


def test_the_close_event_says_a_description_was_cut_and_what_it_cost(db, monkeypatch):
    capture = EventCapture(db).install(monkeypatch)

    _node, result = _closed_into(db, LARGE, author="reviewer")

    carried, measured, cut = _carried(capture.close_events()[-1])
    assert (carried, cut) == (True, result[KEY]["cut"]) == (True, True)
    assert measured == size(result[KEY]["text"])
    assert BRIEF_CAP - 8 <= measured <= BRIEF_CAP


def test_the_close_event_says_nothing_was_carried_for_the_executors_own_task(db, monkeypatch):
    capture = EventCapture(db).install(monkeypatch)

    _closed_into(db, SMALL, author="owner")

    assert _carried(capture.close_events()[-1]) == (False, 0, False)


def test_the_close_event_measures_the_description_a_parked_task_returns_with(db, monkeypatch):
    capture = EventCapture(db).install(monkeypatch)

    _node_id, result = _taken_back_in_a_close(db)

    assert _carried(capture.close_events()[-1]) == (True, size(result[KEY]["text"]), False)


def test_a_description_that_cannot_be_measured_leaves_the_close_event_whole(db, monkeypatch):
    """The three description properties are unknown; every other property is still sent."""
    capture = EventCapture(db).install(monkeypatch)

    def malformed(_result):
        raise KeyError("text")

    monkeypatch.setattr(handoff_brief, "measured", malformed)

    _closed_into(db, SMALL, author="reviewer")

    [event] = capture.close_events()
    assert _carried(event) == (None, None, None)
    assert set(event["properties"]) == {
        "idle", "operation", "named", "selected_how", "open_total", "open_listed",
        "executor_is_actor", *CARRIED,
    }
    assert event["properties"]["selected_how"] == "auto_selected"


def test_a_description_that_cannot_be_measured_is_logged(db, monkeypatch, caplog):
    """Only a malformed fact reaches the handler, so its nulls never pass unseen."""
    EventCapture(db).install(monkeypatch)

    def malformed(_result):
        raise KeyError("text")

    monkeypatch.setattr(handoff_brief, "measured", malformed)

    with caplog.at_level("WARNING", logger="plans.close_next_event"):
        _closed_into(db, SMALL, author="reviewer")

    [record] = [
        record for record in caplog.records if record.name == "plans.close_next_event"
    ]
    assert record.levelname == "WARNING"
    assert "mark_done" in record.getMessage() and "'text'" in record.getMessage()
    assert record.exc_info is not None and record.exc_info[0] is KeyError


def test_a_description_that_only_quotes_the_cut_marker_is_not_reported_cut(db, monkeypatch):
    """The cut is read from the carried text, so an author's own words must not pass for it."""
    capture = EventCapture(db).install(monkeypatch)
    quoted = "Feedback: the old text ended " + _cut_marker("another-node")

    _node, result = _closed_into(db, quoted, author="reviewer")

    assert result[KEY] == _whole(result[KEY]["node_id"], quoted)
    assert _carried(capture.close_events()[-1]) == (True, size(quoted), False)
