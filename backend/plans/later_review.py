"""The weekly review of one plan's Later pile: its arm, its due read, its settle.

``plans.later_review_at`` is the one home of the review time, and only two
writes touch it: ``arm``, made inside the transaction of the first write that
puts a task below the cut, and ``settle``, which answers a review whose time
has come. Nothing else moves it at run time -- a second, tenth or hundredth task
crossing the cut leaves it where the first put it -- so a plan keeps one review
weekday however big its pile grows, and a late sweep cannot start a second
review. The one other writer is the one-time upgrade
``infra.migration_195_to_196_later_review_arm``, which arms plans whose pile
predates the review the way ``arm`` would.

Nothing is scheduled for the review and nothing is derived from a timestamp:
the continuation runner asks ``due`` on its own cadence, and ``settle`` answers
for the plan in one write -- one wake to its owner, then next week's time, or
no wake and NULL when the pile holds no open task. Because the pile is counted
at settle and never stored, a pile emptied by promotion, close or any other
route wakes nobody; because a null time is only the absence of a pending
review, the next task below the cut arms the plan again.

The pile's rows come from ``plans.node_order``, so the wake, the ``later``
section read and the pane's Later group are cut from one order, and a row's
reason and date come from ``row_facts``, so the same row reads the same on both
surfaces.
"""

from __future__ import annotations

from datetime import timedelta

from infra.packed_rows import fetch_all
from plans import (
    later_fields, node_order, node_waits, plan_wake, service, status_time,
)
from plans.close_next_bounds import CRITERIA_MIN_BYTES, bounded, json_bytes, title as _title
from plans.errors import PlanError
from plans.node_waits import encoded_ids
from prompts import later_pull
from prompts import later_review as text
from prompts.plan_tools import bounded_text

#: One week between reviews of the same pile. A plan's review weekday is set by
#: its first task below the cut, so plans spread across the week on their own.
REVIEW_DAYS = 7
#: A row inside the wake identifies the task; reading it is the section read's
#: job, so its reason is shorter than its status counterpart. A title is cut
#: like every plan title (``close_next_bounds.TITLE_MAX_BYTES``).
WAKE_REASON_BYTES = 60
#: Rows one wake lists before it points at the section read instead. The wake
#: needs no byte cap of its own: it is at most this many rows, each bounded by
#: its title and reason cuts and its done-when line (``CRITERIA_MIN_BYTES``),
#: plus the count and the moves.
WAKE_ROWS = 8

#: The latest move below the cut, including a move with no reason, per row.
_REASON_SQL = """SELECT e.node_id AS node_id,
        json_extract(e.payload_json,'$.reason') AS reason
    FROM plan_node_events AS e
    WHERE e.event=? AND e.node_id IN (SELECT value FROM json_each(?))
      AND e.rowid=(SELECT MAX(l.rowid) FROM plan_node_events AS l
                   WHERE l.node_id=e.node_id AND l.event=?
                     AND json_extract(l.payload_json,'$.later')=1)
    """


def _week_out(instant: str) -> str:
    """One week after ``instant``, in the same canonical form.

    Every value the field holds is written here, so ``due`` compares like with
    like rather than parsing a row each sweep; a value that does not parse is a
    defect in the caller, not input.
    """
    parsed = status_time.parse(instant)
    if parsed is None:
        raise PlanError(f"not a timestamp: {instant!r}")
    return (parsed + timedelta(days=REVIEW_DAYS)).isoformat()


def arm(connection, plan_id: str) -> None:
    """Give a plan its first review, a week out, on the write that needs it.

    A plan whose review is already armed is left exactly where it is: the
    cadence belongs to the pile, not to the task that arrived last.
    """
    connection.execute(
        "UPDATE plans SET later_review_at=? WHERE id=? AND owner_kind='thread' AND later_review_at IS NULL",
        (_week_out(service._now()), str(plan_id)),
    )


def arm_node(connection, node_id: str) -> None:
    """Arm the plan holding this node, for a crossing that has the node and not the plan."""
    connection.execute(
        """UPDATE plans SET later_review_at=?
           WHERE later_review_at IS NULL AND owner_kind='thread'
             AND id=(SELECT plan_id FROM plan_nodes WHERE id=?)""",
        (_week_out(service._now()), str(node_id)),
    )


def due(db, now: str) -> list:
    """The plans whose Later review has come due: active, unpaused, owned by a thread.

    A paused plan is never due -- nothing is collected while its owner is
    stopped -- and its overdue review waits for the resume rather than stacking
    up behind it. A plan owned by a scope rather than a thread is out too: the
    review is a wake, and there is no thread to wake; its pile reads in the pane.
    """
    return db.execute(
        """SELECT id, owner_id, later_review_at FROM plans
           WHERE state='active' AND owner_kind='thread'
             AND later_review_at IS NOT NULL AND later_review_at<=?
           ORDER BY later_review_at, id""",
        (now,),
    ).fetchall()


def pile(db, plan_id: str) -> list[dict]:
    """The plan's open tasks below the cut, in plan order: what a row is made of.

    The read costs the open work, never the settled history behind it. Only a
    task carries the cut (``plans.later_fields``), and a branch's Later is
    derived from its tasks, so the tasks are the whole pile.
    """
    return [
        {"id": str(row["id"]), "title": str(row["title"]), "revision": int(row["revision"])}
        for row, _path in node_order.open_nodes_with_paths(db, str(plan_id))
        if row["kind"] == "task" and row["later"]
    ]


def row_facts(db, node_ids) -> dict[str, dict]:
    """Each listed row's Later reason and its date, read for those rows and no others.

    The reason is the latest one that moved the task below the cut; the date is
    its earliest armed deadline watch, which is what makes a dated wait. A row
    with neither still belongs to the pile -- the move itself is the record.
    """
    ids = list(dict.fromkeys(str(node_id) for node_id in node_ids))
    if not ids:
        return {}
    facts: dict[str, dict] = {}
    for row in fetch_all(db, _REASON_SQL, (later_fields.EVENT, encoded_ids(ids), later_fields.EVENT)):
        if row["reason"] is not None:
            facts.setdefault(str(row["node_id"]), {})["reason"] = row["reason"]
    for node_id, watches in node_waits.armed_watches(db, ids, compact=True).items():
        dated = sorted(
            str(row["condition_value"]) for row in watches if row["condition_type"] == "deadline"
        )
        if dated:
            facts.setdefault(node_id, {})["date"] = dated[0][:10]
    return facts


def criteria_text(criteria, node_id: str) -> str:
    """What a Later task must deliver, as the line where its fit is judged.

    Bounded like a selected task's criteria in a wake, and never cut silently:
    a cut ends by naming the read that returns the whole task.
    """
    marker = later_pull.CRITERIA_CUT.format(node_id=node_id)
    return later_pull.CRITERIA.format(criteria=bounded_text(
        str(criteria or "").strip() or later_pull.NO_CRITERIA.format(node_id=node_id),
        CRITERIA_MIN_BYTES, measure=json_bytes, marker=marker,
    ))


def _criteria(db, node_ids) -> dict[str, str]:
    """The listed rows' acceptance criteria, read for those rows and no others."""
    return {
        str(row["id"]): row["acceptance_criteria"]
        for row in fetch_all(
            db, "SELECT id, acceptance_criteria FROM plan_nodes WHERE id IN (SELECT value FROM json_each(?))",
            (encoded_ids(list(node_ids)),),
        )
    }


def _line(row: dict, extra: dict, criteria=None) -> str:
    """One row of the wake: identity to act on, its date or its reason, then what it delivers."""
    reason = extra.get("reason")
    if extra.get("date"):
        tail = text.LATER_REVIEW_UNTIL.format(until=extra["date"])
    elif reason:
        tail = text.LATER_REVIEW_REASON.format(reason=bounded(str(reason), WAKE_REASON_BYTES))
    else:
        tail = ""
    line = text.LATER_REVIEW_ROW.format(
        title=_title(str(row["title"])),
        node_id=row["id"], revision=row["revision"], tail=tail,
    )
    return f"{line}\n  {criteria_text(criteria, row['id'])}"


def wake_text(db, rows: list[dict]) -> str:
    """The wake itself: the count, the rows in plan order, the rest, the three moves.

    The count is the whole pile's and the rows are the first ``WAKE_ROWS`` in
    plan order, so a wake past that still says how big the pile is and names
    where the remainder is.
    """
    head = text.LATER_REVIEW_HEAD.format(
        count=len(rows), task="task" if len(rows) == 1 else "tasks",
    )
    ids = [row["id"] for row in rows[:WAKE_ROWS]]
    extra, criteria = row_facts(db, ids), _criteria(db, ids)
    shown = [_line(row, extra.get(row["id"], {}), criteria.get(row["id"])) for row in rows[:WAKE_ROWS]]
    closing = [text.LATER_REVIEW_MORE.format(count=len(rows) - len(shown))] if len(shown) < len(rows) else []
    return "\n".join([head, *shown, *closing, text.LATER_REVIEW_MOVES])


def reviews(connection, *, now: str, plan_ids) -> None:
    """Answer each swept plan's review, on the write the caller already opened.

    One write for the whole sweep, as every other ``prepare`` source does: a
    plan re-checked there and no longer due contributes nothing.
    """
    for plan_id in plan_ids:
        settle(connection, plan_id, now=now)


def settle(connection, plan_id: str, *, now: str) -> str | None:
    """Answer one plan's due review on an open write: one wake, then next week's arm.

    The plan is read again here, so a pause, a completion or a re-arm that won
    the race cancels the review instead of waking a stopped owner. An empty pile
    clears the arm and wakes nobody; a non-empty one sends one wake whose
    identity carries the due time, then arms the next week in the same write, so
    a sweep that repeats before the next review appends nothing.

    Returns the wake's durable id, or None when nothing was sent.
    """
    plan = connection.execute(
        "SELECT owner_id, state, owner_kind, later_review_at FROM plans WHERE id=?",
        (str(plan_id),),
    ).fetchone()
    if (plan is None or plan["state"] != "active" or plan["owner_kind"] != "thread"
            or plan["later_review_at"] is None or str(plan["later_review_at"]) > now):
        return None
    rows = pile(connection, plan_id)
    if not rows:
        _arm_again(connection, plan_id, None)
        return None
    wake = plan_wake.append(
        connection, recipient=str(plan["owner_id"]), kind=plan_wake.LATER_REVIEW,
        subject_id=str(plan_id), revision=str(plan["later_review_at"]),
        plan_id=str(plan_id), content=wake_text(connection, rows),
    )
    _arm_again(connection, plan_id, _week_out(now))
    return wake


def _arm_again(connection, plan_id: str, instant: str | None) -> None:
    """Move the review to ``instant``, or to nothing when the pile is empty."""
    connection.execute(
        "UPDATE plans SET later_review_at=? WHERE id=?", (instant, str(plan_id)),
    )
