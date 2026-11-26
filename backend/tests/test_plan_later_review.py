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
from prompts.later_review import LATER_REVIEW_MOVES
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


def _fail_task(db, node_id):
    service.transition_node(node_id, "failed", db=db)


def test_last_above_line_settlement_brings_review_due_once(db):
    plan = owner_plan(db)
    top = task(db, plan.id, "Current work")
    _below_cut(db, plan.id, "Someday")
    db.commit()
    _fail_task(db, top.id)
    now = service._now()
    assert _review_at(db, plan.id) <= now
    assert [r["id"] for r in later_review.due(db, now)] == [plan.id]
    _sweep(db, now)
    assert len(_wakes(db)) == 1
    assert later_review.due(db, now) == []
    _sweep(db, now)
    assert len(_wakes(db)) == 1


def _reviewed_once(db, plan_id: str) -> str:
    """Empty the top over a pile, send its early review, and return the review time."""
    top = task(db, plan_id, "Current work")
    db.commit()
    _fail_task(db, top.id)
    now = service._now()
    _sweep(db, now)
    assert len(_wakes(db)) == 1
    assert db.execute(
        "SELECT later_reviewed_at FROM plans WHERE id=?", (plan_id,)
    ).fetchone()["later_reviewed_at"] == now
    return now


def _top_empties_again(db, plan_id: str) -> list:
    top = task(db, plan_id, "Next work")
    db.commit()
    _fail_task(db, top.id)
    return [r["id"] for r in later_review.due(db, service._now())]


def test_no_repeat_review_on_an_unchanged_pile(db):
    plan = owner_plan(db)
    _below_cut(db, plan.id, "Someday")
    _reviewed_once(db, plan.id)
    assert _top_empties_again(db, plan.id) == []


def test_repeat_review_when_a_task_is_added_below_the_cut(db):
    plan = owner_plan(db)
    _below_cut(db, plan.id, "Someday")
    _reviewed_once(db, plan.id)
    _below_cut(db, plan.id, "New someday")
    assert _top_empties_again(db, plan.id) == [plan.id]


def test_repeat_review_when_a_task_is_moved_below_the_cut(db):
    plan = owner_plan(db)
    _below_cut(db, plan.id, "Someday")
    moved = _below_cut(db, plan.id, "Created before the review")
    _reviewed_once(db, plan.id)
    for later, reason in ((False, None), (True, "Not now")):
        plan_update(db, plan.id, "edit", {
            "node_id": moved["id"], "expected_revision": revision(db, moved["id"]),
            "later": later, **({"reason": reason} if reason else {}),
        })
    db.commit()
    assert _top_empties_again(db, plan.id) == [plan.id]


def test_repeat_review_when_priorities_change_and_not_before(db, tmp_path, monkeypatch):
    import os
    from infra import config

    monkeypatch.setattr(config, "WORKSPACE", str(tmp_path))
    priorities = tmp_path / ".threads" / "owner" / "context" / "PRIORITIES.md"
    priorities.parent.mkdir(parents=True)
    priorities.write_text("1. Ship the release\n")
    plan = owner_plan(db)
    _below_cut(db, plan.id, "Someday")
    reviewed = datetime.fromisoformat(_reviewed_once(db, plan.id)).timestamp()
    os.utime(priorities, (reviewed - 3600, reviewed - 3600))
    assert _top_empties_again(db, plan.id) == []
    os.utime(priorities, (reviewed + 60, reviewed + 60))
    assert _top_empties_again(db, plan.id) == [plan.id]


def test_project_priorities_written_after_review_count_as_changed(db, tmp_path, monkeypatch):
    import os
    from core.workspace_layout import resolve_project_root
    from infra import config
    from infra.platform_paths import WorkspaceSelection

    workspace = tmp_path / "workspace"
    monkeypatch.setattr(config, "WORKSPACE", str(workspace))
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", WorkspaceSelection(
        workspace_id="test-workspace", root=workspace.resolve(), name="Test workspace",
    ))
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('proj','Proj','t','t')")
    db.execute("UPDATE threads SET project_id='proj' WHERE id='owner'")
    db.commit()
    (workspace / "projects" / "proj").mkdir(parents=True)
    assert db.execute("SELECT project_id FROM threads WHERE id='owner'").fetchone()["project_id"] == "proj"
    root = resolve_project_root(workspace, "proj", db=db)
    assert root is not None and root != workspace.resolve()
    priorities = root / "context" / "PRIORITIES.md"
    priorities.parent.mkdir(parents=True)
    priorities.write_text("1. Ship the release\n")
    reviewed = service._now()
    at = datetime.fromisoformat(reviewed).timestamp()
    os.utime(priorities, (at - 3600, at - 3600))
    assert later_review._priorities_written_after(db, "owner", reviewed) is False
    os.utime(priorities, (at + 60, at + 60))
    assert later_review._priorities_written_after(db, "owner", reviewed) is True


def test_unreadable_priorities_file_is_logged_not_dropped(db, tmp_path, monkeypatch, caplog):
    import errno
    from pathlib import Path

    path = tmp_path / "PRIORITIES.md"
    monkeypatch.setattr(
        "registry.priorities_ownership.agent_priorities_files",
        lambda thread_id, *, workspace, db: [path],
    )
    real_stat = Path.stat

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError(errno.EACCES, "denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    with caplog.at_level("WARNING", logger="plans.later_review"):
        assert later_review._priorities_written_after(db, "owner", service._now()) is False
    assert any("PRIORITIES.md" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level("WARNING", logger="plans.later_review"):
        monkeypatch.setattr(Path, "stat", real_stat)
        assert later_review._priorities_written_after(db, "owner", service._now()) is False
    assert caplog.records == []


def test_weekly_review_is_sent_on_an_unchanged_pile(db):
    plan = owner_plan(db)
    _below_cut(db, plan.id, "Someday")
    _reviewed_once(db, plan.id)
    _arm(db, plan.id, DUE)
    _sweep(db)
    assert len(_wakes(db)) == 2


def test_review_is_armed_only_after_commit_and_not_after_rollback(db):
    from infra.database import write_transaction
    import pytest

    plan = owner_plan(db)
    top = task(db, plan.id, "Current work")
    _below_cut(db, plan.id, "Someday")
    db.commit()
    before = _review_at(db, plan.id)
    with pytest.raises(RuntimeError, match="rollback"):
        with write_transaction(db, scope="test_later_rollback"):
            _fail_task(db, top.id)
            assert _review_at(db, plan.id) == before
            raise RuntimeError("rollback")
    assert _review_at(db, plan.id) == before
    assert row(db, top.id)["state"] == "pending"
    with write_transaction(db, scope="test_later_commit"):
        _fail_task(db, top.id)
        assert _review_at(db, plan.id) == before
    assert _review_at(db, plan.id) <= service._now()


def test_unscoped_settlement_keeps_weekly_review(db, caplog):
    plan = owner_plan(db)
    top = task(db, plan.id, "Current work")
    _below_cut(db, plan.id, "Someday")
    db.commit()
    before = _review_at(db, plan.id)
    db.execute("UPDATE plans SET later_review_at=later_review_at WHERE id=?", (plan.id,))
    with caplog.at_level("WARNING", logger="plans.later_review"):
        _fail_task(db, top.id)
    db.commit()
    assert row(db, top.id)["state"] == "failed"
    assert _review_at(db, plan.id) == before
    assert any(
        record.levelname == "WARNING" and plan.id in record.getMessage()
        and "early Later review not scheduled" in record.getMessage()
        for record in caplog.records
    )


def test_settlement_with_no_later_does_not_arm(db):
    plan = owner_plan(db)
    top = task(db, plan.id, "Current work")
    _fail_task(db, top.id)
    assert _review_at(db, plan.id) is None
    assert later_review.due(db, service._now()) == []


def test_settlement_with_another_above_line_task_keeps_time(db):
    plan = owner_plan(db)
    top = task(db, plan.id, "Current work")
    task(db, plan.id, "Other work")
    _below_cut(db, plan.id, "Someday")
    before = _review_at(db, plan.id)
    db.commit()
    _fail_task(db, top.id)
    assert _review_at(db, plan.id) == before


def test_settlement_of_later_task_does_not_rearm(db):
    plan = owner_plan(db)
    one = _below_cut(db, plan.id, "Someday")
    _below_cut(db, plan.id, "Other someday")
    before = _review_at(db, plan.id)
    _fail_task(db, one["id"])
    assert _review_at(db, plan.id) == before


def test_last_settlement_paused_plan_is_not_due(db):
    plan = owner_plan(db)
    top = task(db, plan.id, "Current work")
    _below_cut(db, plan.id, "Someday")
    lifecycle.transition(plan.id, "paused", expected_revision=service._plan_row(db, plan.id)["revision"],
                         actor_id="user", provenance="test", db=db)
    db.commit()
    _fail_task(db, top.id)
    assert _review_at(db, plan.id) <= service._now()
    assert later_review.due(db, service._now()) == []


def test_last_settlement_scope_plan_does_not_arm(db):
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','Project','t','t')")
    db.commit()
    plan = service.ensure_plan("project_scope", "p", db=db)
    top = task(db, plan.id, "Current work")
    task(db, plan.id, "Someday", later=True)
    _fail_task(db, top.id)
    assert _review_at(db, plan.id) is None


def test_accept_with_follow_up_keeps_review_time(db):
    from plans import mutations

    plan = owner_plan(db)
    top = task(db, plan.id, "Current work")
    _below_cut(db, plan.id, "Someday")
    db.commit()
    service.transition_node(top.id, "active", db=db)
    mutations.request_review(top.id, evidence="verified", actor_id="owner", db=db)
    before = _review_at(db, plan.id)
    _, follow_up = service.accept_with_follow_up(
        top.id, title="Follow up", acceptance_criteria="Done", verification_result="passed",
        verified_by="test", db=db,
    )
    assert not row(db, follow_up.id)["later"]
    assert _review_at(db, plan.id) == before


def test_send_back_revision_keeps_review_time(db):
    from plans import rejection

    plan = owner_plan(db)
    top = task(db, plan.id, "Current work")
    _below_cut(db, plan.id, "Someday")
    db.commit()
    service.transition_node(top.id, "active", db=db)
    service.transition_node(top.id, "review_requested", db=db)
    before = _review_at(db, plan.id)
    _, revision_node, _ = rejection.reject_submission(top.id, feedback="Revise", db=db)
    assert not row(db, revision_node.id)["later"]
    assert _review_at(db, plan.id) == before


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


def test_bulk_edit_arms_the_first_later_review(db):
    plan = owner_plan(db)
    nodes = [task(db, plan.id, f"Someday {index}") for index in range(2)]
    result = plan_update(db, plan.id, "edit", {
        "node_ids": [node.id for node in nodes], "later": True,
    })
    assert len(result["changed"]) == 2 and result["refused"] == []
    assert 6.999 < _days_from_now(_review_at(db, plan.id), clock=service._now()) <= 7


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


def test_the_wake_lists_the_first_rows_of_the_pile_and_counts_the_rest(db):
    plan = owner_plan(db)
    pile = _pile_below(db, plan.id, 40, reason="Not before the launch cut")
    _arm(db, plan.id, DUE)

    _sweep(db)

    content = _wakes(db)[0]["content"]
    assert "40" in content
    assert pile[0]["title"] in content and pile[0]["id"] in content
    assert content.count("\n- ") == later_review.WAKE_ROWS
    omitted = 40 - later_review.WAKE_ROWS
    assert f"+{omitted} more: plan_read(section='later')" in content


def test_review_wake_bulk_close_uses_the_real_plan_boundary(db):
    plan = owner_plan(db)
    pile = _pile_below(db, plan.id, 3)
    _arm(db, plan.id, DUE)
    _sweep(db)
    assert "one close_node call with node_ids" in _wakes(db)[0]["content"]

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
    assert " until " not in content.replace(LATER_REVIEW_MOVES, "")
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



def test_a_review_time_that_does_not_parse_is_refused_not_replaced():
    try:
        later_review._week_out("not a time")
    except PlanError as exc:
        assert "not a timestamp" in str(exc)
    else:
        raise AssertionError("a malformed instant must not fall back to the wall clock")
