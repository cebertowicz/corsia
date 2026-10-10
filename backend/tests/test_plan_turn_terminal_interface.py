"""``plans.tool_dispatches.settle_turn`` called directly inside a terminal write.

``infra.thread_driver`` calls ``settle_turn(turn_id, *, db) -> int`` inside its
terminal write and carries the returned count of calls it settled as unknown. These tests call it the same way with a dispatch whose Turn row
is gone; the behavior through ``finish_turn`` lives in the sibling
``test_plan_turn_terminal_*`` modules.
"""

from __future__ import annotations

from infra.database import write_transaction
from plans import admission, service, tool_dispatches
from tests.test_clean_turn_terminal import _db, _session
from tests.test_plan_turn_terminal_settlement import (
    THREAD,
    _attempt,
    _dispatch,
    _open_attempt,
    _open_dispatch,
    _selected_task,
    _settle_task,
    _task,
)


def _settle_turn_in_terminal_write(db, turn_id: str):
    """Settle a Turn's dispatches inside a write, as ``finish_turn`` does."""
    with write_transaction(db, scope="turn_terminal_interface"):
        return tool_dispatches.settle_turn(turn_id, db=db)


def test_settle_turn_keeps_a_valid_selection_and_is_idempotent() -> None:
    """Orphaned calls close as unknown by Turn id; valid work stays selected."""
    db = _db()
    try:
        _session(db, THREAD)
        _plan, task, attempt_id = _selected_task(db, THREAD)
        _open_dispatch(db, attempt_id, "turn-crashed")

        assert _settle_turn_in_terminal_write(db, "turn-crashed") == 1
        settled = tuple(_dispatch(db))
        assert _settle_turn_in_terminal_write(db, "turn-crashed") == 0

        assert settled[0] == "unknown"
        assert tuple(_dispatch(db)) == settled
        assert _attempt(db, attempt_id)["released_at"] is None
        assert db.execute("SELECT COUNT(*) FROM plan_node_holds").fetchone()[0] == 0
        assert admission.project(task.id, db=db)["condition"] == "running"
    finally:
        db.close()


def test_settle_turn_after_a_park_only_closes_the_call() -> None:
    """The park already released the attempt and handed the slot on (#2705).

    Settling the Turn closes the orphaned call as unknown under the released
    attempt and leaves the successor's selection alone.
    """
    db = _db()
    try:
        _session(db, THREAD)
        plan, task_a, attempt_a = _selected_task(db, THREAD)
        task_b = _task(db, plan.id, "Task B", 1)
        service.transition_node(task_b.id, "active", db=db)
        _open_dispatch(db, attempt_a, "turn-crashed")
        _settle_task(db, plan.id, task_a.id, "request_review")
        successor = _open_attempt(db, task_b.id)
        assert successor is not None

        assert _settle_turn_in_terminal_write(db, "turn-crashed") == 1

        assert _dispatch(db)["outcome"] == "unknown"
        assert _attempt(db, attempt_a)["release_reason"] == "review_requested"
        assert _open_attempt(db, task_b.id)["id"] == successor["id"]
        assert admission.project(task_b.id, db=db)["condition"] == "running"
        assert db.execute("SELECT COUNT(*) FROM plan_node_holds").fetchone()[0] == 0
    finally:
        db.close()


def test_settle_turn_releases_a_selection_left_on_a_closed_task() -> None:
    """A task closed while its release was deferred never refuses settlement.

    Before #2705 a close with a call in flight left the attempt open on the
    closed task. Settling that Turn at restart released it, then offered the
    closed task its slot back and raised ``plan node is closed`` inside the
    terminal write, so startup recovery aborted. The retired task is now only
    released and its plan reconciled, handing the slot to queued work.
    """
    db = _db()
    try:
        _session(db, THREAD)
        plan, task_a, attempt_a = _selected_task(db, THREAD)
        task_b = _task(db, plan.id, "Task B", 1)
        service.transition_node(task_b.id, "active", db=db)
        _open_dispatch(db, attempt_a, "turn-crashed")
        # Pre-#2705 shape: closed with the attempt still open.
        db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (task_a.id,))
        db.commit()

        assert _settle_turn_in_terminal_write(db, "turn-crashed") == 1

        assert _dispatch(db)["outcome"] == "unknown"
        assert _attempt(db, attempt_a)["release_reason"] == "turn_terminal"
        assert _open_attempt(db, task_a.id) is None
        assert _open_attempt(db, task_b.id) is not None
    finally:
        db.close()
