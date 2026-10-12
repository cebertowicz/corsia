"""The `next` block is byte-bounded without ever cutting an id or a revision.

Titles, waits and the named reason are cut to their serialized-byte caps
(measured as the tool emits them, `ensure_ascii=True`) at a code-point boundary
with a trailing marker. The selected task's acceptance criteria take every byte
the rest of the block leaves, so they arrive whole whenever the block can hold
them; criteria longer than that end with a marker no author writes, naming the
call that returns them whole. With today's id generators (at most 36
characters) the worst case - three listed rows, an auto-selected task with long
criteria and a refused named start, every text at its cap and every revision ten
digits long - still fits within 2 KiB with no row dropped. Only pathological
legacy ids make the block drop listed rows from the tail, and
`open_tasks_listed` then describes exactly what is shown.
"""

from __future__ import annotations

import uuid

import pytest

from plans import service
from plans.boundary import PlanActor
from tests.plan_close_next_helpers import (
    CRITERIA_MARKER,
    NEXT_BLOCK_CAP,
    REASON_CAP,
    TITLE_CAP,
    WAIT_CAP,
    assert_block_shape,
    assert_rows_exact,
    assert_truncated,
    close,
    listed,
    next_block,
    open_attempt,
    owner_plan,
    pause,
    plan_update,
    queue_active,
    revision,
    row,
    row_ids,
    select,
    size,
    task,
)
from tests.test_plan_reconcile_release import db  # noqa: F401 - fixture
from tests.plan_writer_authorizer import raw_plan_seed

EMOJI = "\U0001F600"
QUOTE_RUN = '"\\' * 20
LONG_REVISION = 9_876_543_210


def _uuid_string_id() -> str:
    """Return a 36-character uuid string, the longest id generator in use."""
    return str(uuid.uuid4())


def _pathological_id() -> str:
    """Return a unique 600-character legacy id."""
    return (uuid.uuid4().hex * 19)[:600]


def _uuid_executor(db) -> PlanActor:
    """Seed a plan-owner thread whose id is a 36-character uuid string."""
    thread_id = _uuid_string_id()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?, 'user', 'test/model', 't', 't')",
        (thread_id,),
    )
    db.commit()
    return PlanActor(thread_id, "user", None)


def _legacy(db, node_id: str, **columns) -> None:
    """Write legacy values the authoring boundary would refuse today."""
    with raw_plan_seed(db):
        for column, value in columns.items():
            db.execute(f"UPDATE plan_nodes SET {column}=? WHERE id=?", (value, node_id))
    db.commit()


def _worst_case_scene(db, monkeypatch):
    """Return (actor, plan, task A, queued task, open tasks, titles, hold reason)."""
    monkeypatch.setattr(service, "_id", _uuid_string_id)
    actor = _uuid_executor(db)
    plan = service.ensure_plan("thread", actor.thread_id, db=db)
    task_a = task(db, plan.id, "Task A")
    queued = task(db, plan.id, "Queued work")
    others = [task(db, plan.id, f"Open task {index}") for index in range(10)]
    select(db, plan.id, task_a.id, actor=actor)
    queue_active(db, queued.id)
    reason = '"\\' * 500
    for node in others[:3]:
        queue_active(db, node.id)
        pause(db, plan.id, node.id, reason=reason, actor=actor)
    # Ready work leads the listing, so the rest wait too and the long rows stay listed.
    for node in others[3:]:
        queue_active(db, node.id)
        pause(db, plan.id, node.id, reason="later", actor=actor)
    titles = {
        others[0].id: EMOJI * 60 + QUOTE_RUN,
        others[1].id: QUOTE_RUN + EMOJI * 60,
        others[2].id: ("x" + EMOJI) * 50,
        queued.id: EMOJI * 60 + "y" * 40,
    }
    for node_id, title in titles.items():
        _legacy(db, node_id, title=title, revision=LONG_REVISION)
    _legacy(db, queued.id, acceptance_criteria="z" * 5000)
    return actor, plan, task_a, queued, others, titles, reason


def test_the_worst_case_block_keeps_every_row_and_cuts_text_by_serialized_bytes(
    db, monkeypatch,
):
    actor, plan, task_a, queued, others, titles, reason = _worst_case_scene(db, monkeypatch)

    block = next_block(close(
        db, plan.id, "request_review", task_a.id, next_node_id=others[0].id, actor=actor,
    ))

    assert_block_shape(block, named=True)
    assert size(block) <= NEXT_BLOCK_CAP
    assert block["executor_thread_id"] == actor.thread_id
    assert row_ids(block) == [node.id for node in others[:3]]
    assert block["open_tasks_listed"] == listed(3, 10)
    assert all(len(node_id) == 36 for node_id in row_ids(block))
    assert {item["revision"] for item in block["open_tasks"]} == {LONG_REVISION}
    assert_rows_exact(db, block)
    selected = block["selected"]
    assert (selected["id"], selected["how"]) == (queued.id, "auto_selected")
    for item in [*block["open_tasks"], selected]:
        assert_truncated(item["title"], titles[item["id"]], TITLE_CAP)
    for item in block["open_tasks"]:
        assert_truncated(item["wait"], "paused: " + reason, WAIT_CAP)
    named = block["named"]
    assert (named["started"], named["refusal"]) == (False, "not_ready")
    assert_truncated(named["reason"], "paused: " + reason, REASON_CAP)
    _assert_criteria_fill_the_block(block, "z" * 5000)


def _assert_criteria_fill_the_block(block: dict, stored: str) -> None:
    """Assert cut criteria end with the criteria marker and use every byte left."""
    criteria = block["selected"]["acceptance_criteria"]
    assert criteria.endswith(CRITERIA_MARKER)
    kept = criteria[: -len(CRITERIA_MARKER)]
    assert kept and stored.startswith(kept)
    assert size(block) <= NEXT_BLOCK_CAP
    fuller = kept + stored[len(kept)] + CRITERIA_MARKER
    assert size(dict(block, selected=dict(block["selected"], acceptance_criteria=fuller))) > (
        NEXT_BLOCK_CAP
    )


def _authored_criteria() -> str:
    """Return multi-line criteria of about 1.2 KB that end with an authored ellipsis."""
    lines = [
        f'- Criterion {index}: the "{index}" export check passes on every supported harness'
        for index in range(15)
    ]
    return "\n".join([*lines, "- Rows keep their order when written as Parquet…"])


def test_criteria_arrive_whole_whenever_the_block_has_room_for_them(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    criteria = _authored_criteria()
    task_b = service.add_node(
        plan.id, kind="task", title="Task B", acceptance_criteria=criteria, db=db,
    )
    db.commit()
    pending = [task(db, plan.id, f"Pending {index}") for index in range(4)]
    select(db, plan.id, task_a.id)

    block = next_block(close(db, plan.id, "request_review", task_a.id, next_node_id=task_b.id))

    assert_block_shape(block, named=True)
    assert size(criteria) > NEXT_BLOCK_CAP // 2
    assert block["selected"]["acceptance_criteria"] == criteria
    assert row_ids(block) == [node.id for node in pending[:3]]
    assert block["open_tasks_listed"] == listed(3, 4)


def test_criteria_too_long_for_the_block_are_cut_explicitly_and_a_start_returns_them_whole(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    criteria = " ".join(["Every exported row round-trips with its schema intact."] * 90)
    task_b = service.add_node(
        plan.id, kind="task", title="Task B", acceptance_criteria=criteria, db=db,
    )
    db.commit()
    select(db, plan.id, task_a.id)

    block = next_block(close(db, plan.id, "request_review", task_a.id, next_node_id=task_b.id))

    assert_block_shape(block, named=True)
    _assert_criteria_fill_the_block(block, criteria)
    selected = block["selected"]
    attempt = open_attempt(db, task_b.id)["id"]
    started = plan_update(db, plan.id, "start", {
        "node_id": selected["id"], "expected_revision": selected["revision"],
    })
    assert started["node"]["acceptance_criteria"] == criteria
    assert open_attempt(db, task_b.id)["id"] == attempt
    assert revision(db, task_b.id) == selected["revision"]


def test_short_values_are_returned_whole(db):
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    queued = task(db, plan.id, "Sixty-character ASCII titles always fit the ninety-six cap!!")
    select(db, plan.id, task_a.id)
    queue_active(db, queued.id)

    selected = next_block(close(db, plan.id, "request_review", task_a.id))["selected"]

    assert selected["title"] == row(db, queued.id)["title"]
    assert selected["acceptance_criteria"] == row(db, queued.id)["acceptance_criteria"]


@pytest.mark.parametrize("named", [False, True], ids=["without-named", "with-refused-named"])
def test_rows_drop_from_the_tail_until_the_block_fits_and_the_label_follows(
    db, monkeypatch, named,
):
    monkeypatch.setattr(service, "_id", _pathological_id)
    plan = owner_plan(db)
    task_a = task(db, plan.id, "Task A")
    queued = task(db, plan.id, "Queued work")
    pending = [task(db, plan.id, f"Pending {index}") for index in range(4)]
    select(db, plan.id, task_a.id)
    queue_active(db, queued.id)
    extra = {"next_node_id": "no-such-node"} if named else {}

    block = next_block(close(db, plan.id, "request_review", task_a.id, **extra))

    assert size(block) <= NEXT_BLOCK_CAP
    assert block["selected"]["id"] == queued.id
    assert ("named" in block) is named
    shown = len(block["open_tasks"])
    assert shown < 3
    assert row_ids(block) == [node.id for node in pending[:shown]]
    assert block["open_tasks_listed"] == listed(shown, 4)
    assert_rows_exact(db, block)
