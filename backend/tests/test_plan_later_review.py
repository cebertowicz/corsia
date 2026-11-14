"""The weekly review of one plan's Later pile (Later design §6).

The first write that puts a task below the cut arms the plan's review a week
out and nothing else moves it. A due review wakes the plan's owner once, with
the pile and the three moves, and arms the next week; an empty pile clears the
arm and wakes no one; a paused plan is never due and an overdue one is
delivered once after resume. The pile is also one section read. No plan node,
trigger row or schedule row holds any of it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from automation.continuation.sources.later_review import LaterReviewSource
from plans import later_review, lifecycle, service, status, status_cursor, wake_currency
from plans.errors import PlanError
from plans.plan_wake import LATER_REVIEW
from tests.plan_close_next_helpers import (
    OWNER, owner_plan, plan_update, revision, row, task,
)
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixtures

NOW = "2100-01-08T00:00:00+00:00"
#: One review time in the past, so a sweep finds the plan due.
DUE = "2100-01-01T00:00:00+00:00"
#: An armed review ahead of ``NOW``: not due yet.
AHEAD = "2100-02-01T00:00:00+00:00"


def _review_at(db, plan_id: str) -> str | None:
    return db.execute(
        "SELECT later_review_at FROM plans WHERE id=?", (plan_id,)
    ).fetchone()["later_review_at"]


def _arm(db, plan_id: str, value: str) -> None:
    db.execute("UPDATE plans SET later_review_at=? WHERE id=?", (value, plan_id))
    db.commit()


def _below_cut(db, plan_id: str, title: str, **params):
    """Add one task already below the cut, through the real write."""
    reason = params.pop("reason", None)
    node = plan_update(db, plan_id, "add", {
        "title": title, "acceptance_criteria": f"{title} is verified",
        "size": "unknown", "later": reason is None, **params,
    })["node"]
    if reason is not None:
        node = plan_update(db, plan_id, "edit", {
            "node_id": node["id"], "expected_revision": revision(db, node["id"]),
            "later": True, "reason": reason,
        })["node"]
    return node


def _pile_below(db, plan_id: str, count: int, *, reason: str | None = None) -> list:
    return [_below_cut(db, plan_id, f"Someday {i}", **({"reason": reason} if reason else {}))
            for i in range(count)]


def _sweep(db, now: str = NOW) -> list[str]:
    return LaterReviewSource().prepare(now, db=db)


def _wakes(db, thread_id: str = "owner") -> list:
    return db.execute(
        """SELECT id, content, delivery_state FROM messages
           WHERE thread_id=? AND id LIKE ? ORDER BY input_order""",
        (thread_id, f"plan-wake:{LATER_REVIEW}:%"),
    ).fetchall()


def _days_from_now(value: str, *, clock: str) -> float:
    """Whole days between ``clock`` and an armed review time, tolerant of the clock itself."""
    late = datetime.fromisoformat(value)
    early = datetime.fromisoformat(clock)
    return (late - early).total_seconds() / 86400


# --- the arm ---------------------------------------------------------------


def test_the_first_later_write_arms_the_review_a_week_out(db):
    plan = owner_plan(db)
    held = task(db, plan.id, "Someday")
    assert _review_at(db, plan.id) is None

    plan_update(db, plan.id, "edit", {
        "node_id": held.id, "expected_revision": revision(db, held.id),
        "later": True, "reason": "Not before the launch cut",
    })

    assert 6.999 < _days_from_now(_review_at(db, plan.id), clock=service._now()) <= 7


def test_a_task_born_below_the_cut_arms_the_review_too(db):
    plan = owner_plan(db)

    _below_cut(db, plan.id, "Someday")

    assert 6.999 < _days_from_now(_review_at(db, plan.id), clock=service._now()) <= 7


def test_a_second_later_write_never_moves_the_armed_review(db):
    plan = owner_plan(db)
    armed = _below_cut(db, plan.id, "Someday")["id"]
    _arm(db, plan.id, DUE)
    first = _review_at(db, plan.id)

    _below_cut(db, plan.id, "Someday two")
    other = task(db, plan.id, "Above the cut")
    plan_update(db, plan.id, "edit", {
        "node_id": other.id, "expected_revision": revision(db, other.id), "later": True,
    })

    assert _review_at(db, plan.id) == first
    assert row(db, armed)["later"] == 1


def test_scope_owned_plan_never_arms_a_review(db):
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','Project','t','t')")
    plan = service.ensure_plan("project_scope", "p", db=db)
    task(db, plan.id, "Born later", later=True)
    assert _review_at(db, plan.id) is None

    other = task(db, plan.id, "Moved later")
    from plans import later_fields
    later_fields.flip(db, other.id, later=True, reason="later", actor_id=None, provenance="test")
    assert _review_at(db, plan.id) is None


# --- due selection ---------------------------------------------------------


def test_only_an_active_thread_plan_with_an_armed_review_is_due(db):
    owner = owner_plan(db)
    other = service.ensure_plan("thread", "worker", db=db)
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project-1','Project','t','t')")
    scope = service.ensure_plan("project_scope", "project-1", db=db)
    for plan in (owner, other, scope):
        _arm(db, plan.id, DUE)
    later = service.ensure_plan("thread", "sibling", db=db)
    _arm(db, later.id, AHEAD)
    db.commit()

    due = {str(row_["id"]) for row_ in later_review.due(db, NOW)}

    assert due == {owner.id, other.id}  # scope plan (not thread-owned) and a future time are out


def test_a_paused_plan_is_never_due(db):
    plan = owner_plan(db)
    _below_cut(db, plan.id, "Someday")
    _arm(db, plan.id, DUE)

    lifecycle.transition(
        plan.id, "paused", expected_revision=service._plan_row(db, plan.id)["revision"],
        actor_id="user", provenance="test.later_review", db=db,
    )
    db.commit()

    assert [row_["id"] for row_ in later_review.due(db, NOW)] == []


def test_the_review_never_re_fires_inside_the_week(db):
    plan = owner_plan(db)
    _below_cut(db, plan.id, "Someday")
    _arm(db, plan.id, DUE)
    assert later_review.due(db, NOW)

    _sweep(db)

    assert later_review.due(db, NOW) == []
    assert later_review.due(db, (datetime.fromisoformat(NOW) + timedelta(days=6)).isoformat()) == []
    assert [r["id"] for r in later_review.due(
        db, (datetime.fromisoformat(NOW) + timedelta(days=7)).isoformat()
    )] == [plan.id]


# --- the settle ------------------------------------------------------------


def test_a_due_pile_wakes_the_owner_once_and_arms_the_next_week(db):
    plan = owner_plan(db)
    _below_cut(db, plan.id, "Someday", reason="Not before the launch cut")
    _arm(db, plan.id, DUE)

    driven = _sweep(db)

    assert driven == ["owner"]
    wakes = _wakes(db)
    assert len(wakes) == 1
    assert wakes[0]["id"] == f"plan-wake:{LATER_REVIEW}:{plan.id}:{DUE}"
    assert _review_at(db, plan.id) == (
        datetime.fromisoformat(NOW) + timedelta(days=7)
    ).isoformat()


def test_a_second_pass_in_the_same_week_wakes_no_one(db):
    plan = owner_plan(db)
    _below_cut(db, plan.id, "Someday")
    _arm(db, plan.id, DUE)
    assert _sweep(db) == ["owner"]

    again = _sweep(db)

    # The re-arm is what makes the second pass find nothing; the identity is the
    # backstop for a retry that lands before the re-arm commits.
    assert again == []
    assert len(_wakes(db)) == 1
    _arm(db, plan.id, DUE)
    assert _sweep(db) == []
    assert len(_wakes(db)) == 1


def test_an_empty_pile_clears_the_review_and_wakes_no_one(db):
    plan = owner_plan(db)
    settled = _below_cut(db, plan.id, "Someday")
    _arm(db, plan.id, DUE)
    plan_update(db, plan.id, "close_node", {
        "node_id": settled["id"], "expected_revision": revision(db, settled["id"]),
        "reason": "No longer wanted",
    })

    driven = _sweep(db)

    assert driven == []
    assert _wakes(db) == []
    assert _review_at(db, plan.id) is None


def test_a_paused_plan_wakes_no_one_and_resume_delivers_exactly_once(db):
    plan = owner_plan(db)
    _below_cut(db, plan.id, "Someday")
    _arm(db, plan.id, DUE)
    paused = service._plan_row(db, plan.id)["revision"]
    lifecycle.transition(
        plan.id, "paused", expected_revision=paused, actor_id="user",
        provenance="test.later_review", db=db,
    )
    db.commit()

    assert _sweep(db) == []
    assert _review_at(db, plan.id) == DUE

    lifecycle.transition(
        plan.id, "active", expected_revision=service._plan_row(db, plan.id)["revision"],
        actor_id="user", provenance="test.later_review", db=db,
    )
    db.commit()

    assert _sweep(db) == ["owner"]
    assert len(_wakes(db)) == 1
    assert _sweep(db) == []
    assert len(_wakes(db)) == 1


def test_the_review_costs_no_plan_node_and_no_durable_row(db):
    plan = owner_plan(db)
    _below_cut(db, plan.id, "Someday")
    _arm(db, plan.id, DUE)
    nodes = db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0]

    _sweep(db)

    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == nodes
    assert db.execute("SELECT COUNT(*) FROM continuation_triggers").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM automation_schedules").fetchone()[0] == 0


def test_a_review_wake_is_current_when_the_open_work_is_all_later(db):
    """The pile is deliberately not actionable work, so the review is its own reason."""
    plan = owner_plan(db)
    _below_cut(db, plan.id, "Someday")
    _arm(db, plan.id, DUE)

    _sweep(db)
    wake = _wakes(db)[0]

    assert wake["delivery_state"] == "queued"
    assert wake_currency.is_current(
        "owner",
        wake_currency.condition(
            plan_id=plan.id, kind=LATER_REVIEW, subject_id=plan.id, revision=DUE,
        ),
        db=db,
    )


# --- the wake text ---------------------------------------------------------


def test_the_wake_lists_the_pile_inside_two_kilobytes(db):
    plan = owner_plan(db)
    pile = _pile_below(db, plan.id, 40, reason="Not before the launch cut")
    _arm(db, plan.id, DUE)

    _sweep(db)

    content = _wakes(db)[0]["content"]
    assert len(content.encode()) <= later_review.WAKE_MAX_BYTES <= 2048
    assert "40" in content
    assert pile[0]["title"] in content and pile[0]["id"] in content
    omitted = 40 - content.count("\n- ")
    assert f"+{omitted} more: plan_read(section='later')" in content


def test_review_wake_bulk_close_uses_the_real_plan_boundary(db):
    plan = owner_plan(db)
    pile = _pile_below(db, plan.id, 3)
    _arm(db, plan.id, DUE)
    _sweep(db)
    assert "close_node node_ids" in _wakes(db)[0]["content"]

    ids = [node["id"] for node in pile]
    result = plan_update(db, plan.id, "close_node", {
        "node_ids": ids, "reason": "No longer needed",
    })
    assert [entry["node_id"] for entry in result["closed"]] == ids
    assert result["refused"] == []
    assert all(row(db, node_id)["state"] == "closed" for node_id in ids)


def test_the_wake_names_the_three_moves_it_offers(db):
    plan = owner_plan(db)
    _below_cut(db, plan.id, "Someday", reason="Not before the launch cut")
    _arm(db, plan.id, DUE)

    _sweep(db)

    content = _wakes(db)[0]["content"]
    assert "edit later=false" in content and "reorder" in content and "close_node" in content
    assert "Not before the launch cut" in content
    # No dated Later row shows no date, and a dated one does.
    assert " until " not in content
    dated = task(db, plan.id, "Dated set-aside")
    plan_update(db, plan.id, "arm_watch", {
        "node_id": dated.id, "expected_revision": revision(db, dated.id),
        "condition": {"type": "deadline", "value": "2100-03-01T00:00:00+00:00"},
        "reason": "Waiting on the grant",
    })
    plan_update(db, plan.id, "edit", {
        "node_id": dated.id, "expected_revision": revision(db, dated.id), "later": True,
    })
    _arm(db, plan.id, DUE)
    db.execute("DELETE FROM messages WHERE id LIKE ?", (f"plan-wake:{LATER_REVIEW}:%",))
    _sweep(db)

    assert " until 2100-03-01" in _wakes(db)[0]["content"]


# --- the section read ------------------------------------------------------


def test_the_later_section_pages_the_pile_in_plan_order(db):
    plan = owner_plan(db)
    above = task(db, plan.id, "Above the cut")
    pile = [_below_cut(db, plan.id, f"Someday {i}") for i in range(status.LATER_ROWS + 2)]
    row_ = status.read(db, service._plan_row(db, plan.id), actor=OWNER, section="later")

    assert row_["section"] == "later"
    assert row_["total_count"] == len(pile)
    assert [r["id"] for r in row_["rows"]] == [n["id"] for n in pile[:status.LATER_ROWS]]
    assert row_["omitted_count"] == 2 and row_["cursor"]
    assert row_["continue_read"].startswith("plan_read(section='later', cursor='")
    assert all({"id", "revision", "title", "reason", "detail_read"} <= set(r) for r in row_["rows"])
    assert all("state" not in r and "condition" not in r for r in row_["rows"])
    assert above.id not in {r["id"] for r in row_["rows"]}

    rest = status.read(
        db, service._plan_row(db, plan.id), actor=OWNER, section="later",
        cursor=row_["cursor"],
    )
    assert [r["id"] for r in rest["rows"]] == [n["id"] for n in pile[status.LATER_ROWS:]]
    assert "cursor" not in rest and rest["omitted_count"] == 0


def test_the_later_section_row_carries_its_reason_and_date(db):
    plan = owner_plan(db)
    plain = _below_cut(db, plan.id, "Undated", reason="Not before the launch cut")
    dated = _below_cut(db, plan.id, "Dated", reason="Waiting on the grant")
    plan_update(db, plan.id, "arm_watch", {
        "node_id": dated["id"], "expected_revision": revision(db, dated["id"]),
        "condition": {"type": "deadline", "value": "2100-03-01T00:00:00+00:00"},
        "reason": "Waiting on the grant",
    })

    rows = status.read(
        db, service._plan_row(db, plan.id), actor=OWNER, section="later",
    )["rows"]

    by_id = {r["id"]: r for r in rows}
    assert by_id[plain["id"]]["reason"] == "Not before the launch cut"
    assert "date" not in by_id[plain["id"]]
    assert by_id[dated["id"]]["date"] == "2100-03-01"
    assert by_id[dated["id"]]["detail_read"] == f"plan_read(node_id='{dated['id']}')"


def test_latest_later_flip_without_reason_clears_old_reason(db):
    plan = owner_plan(db)
    node = _below_cut(db, plan.id, "Someday", reason="Old reason")
    for flag in (False, True):
        plan_update(db, plan.id, "edit", {
            "node_id": node["id"], "expected_revision": revision(db, node["id"]),
            "later": flag,
        })
    rows = status.read(db, service._plan_row(db, plan.id), actor=OWNER, section="later")["rows"]
    assert not rows[0].get("reason")


def test_a_later_section_cursor_refuses_a_snapshot_that_moved(db):
    plan = owner_plan(db)
    _pile_below(db, plan.id, status.LATER_ROWS + 1)
    first = status.read(
        db, service._plan_row(db, plan.id), actor=OWNER, section="later",
    )
    moved = first["rows"][0]
    plan_update(db, plan.id, "edit", {
        "node_id": moved["id"], "expected_revision": moved["revision"],
        "title": "Renamed under the cut",
    })

    try:
        status.read(
            db, service._plan_row(db, plan.id), actor=OWNER, section="later",
            cursor=first["cursor"],
        )
    except PlanError as exc:
        assert str(exc) == status_cursor.STALE
    else:
        raise AssertionError("a moved pile must refuse the old cursor")


def test_later_is_listed_wherever_sections_are():
    from plans import withheld_projection
    from tools import plan_schemas

    assert "later" in status.SECTIONS
    assert "later" in withheld_projection._ENUM_VALUES["section"]
    assert "later" in plan_schemas.READ_PARAMS["section"]["enum"]

