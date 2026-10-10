"""At the context boundary a thread gets its plan's thin status, never its plan.

Plan identity and revision, progress, the current task and its revision, only
the next step or the genuine wait, and the read that gets exact detail -- at
most 2 KiB. An idle or paused plan says so; nothing guesses a current step.
"""

from __future__ import annotations

import json

from plans import admission, lifecycle, service, status_context
from plans.boundary import PlanActor
from tests.test_plan_status import CLOCK, _db, _plan

WORKER = PlanActor("worker", "user-1", "project-1")


def _thin(db, plan_id: str) -> dict:
    return status_context.projection(db, service._plan_row(db, plan_id), actor=WORKER, clock=CLOCK)


def test_the_projection_fits_two_kib_and_names_the_current_task_and_revision():
    db = _db()
    try:
        plan, nodes = _plan(db)
        row = service._plan_row(db, plan.id)

        thin = _thin(db, plan.id)

        rendered = status_context.render(thin)
        assert len(rendered.encode()) <= status_context.CHECKPOINT_BYTES
        assert thin["plan"] == {"id": plan.id, "revision": row["revision"], "state": "active"}
        assert thin["current_task"]["id"] == nodes["running"].id
        assert thin["current_task"]["revision"] >= nodes["running"].revision
        assert thin["progress"]["total"] == 7  # finished top-level steps folded out
        assert thin["status_read"] == "plan_read()"
        assert "description" not in rendered and "acceptance" not in rendered
        assert "note" not in thin
    finally:
        db.close()


def test_an_idle_plan_and_a_paused_plan_say_so():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "worker", db=db)
        db.commit()
        idle = _thin(db, plan.id)
        assert idle["no_current_task"] is True and idle["next_step"] is None
        assert idle["note"].startswith("idle")

        node = service.add_node(plan.id, kind="task", title="Later", acceptance_criteria="Done", db=db)
        db.commit()
        row = service._plan_row(db, plan.id)
        lifecycle.transition(
            plan.id, "paused", expected_revision=row["revision"], actor_id="user:user-1",
            provenance="test", db=db,
        )
        db.commit()
        paused = _thin(db, plan.id)
        assert paused["plan"]["state"] == "paused"
        assert paused["note"].startswith("not moving: 1 paused by the user.")
        assert paused["no_current_task"] is True
        assert node.id  # the ready step is not guessed as current
    finally:
        db.close()


def test_a_mutation_moves_the_revision_the_projection_carries():
    db = _db()
    try:
        plan, nodes = _plan(db)
        before = _thin(db, plan.id)["plan"]["revision"]
        admission.release(nodes["running"].id, "accepted", db=db)
        db.commit()
        assert _thin(db, plan.id)["plan"]["revision"] >= before
        assert _thin(db, plan.id)["no_current_task"] is True
    finally:
        db.close()


def _digests(db, thread_id: str) -> list[str]:
    return [
        row["content"] for row in db.execute(
            "SELECT content,metadata FROM messages WHERE thread_id=? ORDER BY seq,rowid",
            (thread_id,),
        ).fetchall()
        if json.loads(row["metadata"] or "{}").get("event_type") == "context.digest"
    ]


def _next_input(db, thread_id: str, turn: str) -> None:
    from infra.thread_driver import admit_input

    admit_input(
        thread_id, role="user", content="carry on", intent="apply_now",
        message_id=f"input-{turn}", turn_id=turn, boot_epoch="boot",
        admitted_at="2026-09-12T12:00:00+00:00", db=db,
    )
    db.commit()


def test_a_plan_write_hands_the_owner_its_thin_status_in_the_passive_digest():
    """The projection rides the digest at the next Turn boundary: never the cached
    prefix, never memoized, one line no matter how many writes."""
    db = _db()
    try:
        plan, nodes = _plan(db)
        db.execute(
            """INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
               VALUES ('other','user-1','project-1','test/model','t','t')"""
        )
        db.commit()
        service.transition_node(nodes["pending"][0].id, "active", actor_id="worker", db=db)
        db.commit()

        _next_input(db, "worker", "turn-1")

        digests = _digests(db, "worker")
        assert len(digests) == 1
        lines = [line for line in digests[0].split("\n") if "Current plan status: " in line]
        assert len(lines) == 1, digests[0]
        thin = json.loads(lines[0].split("Current plan status: ", 1)[1])
        assert thin["plan"]["id"] == plan.id
        assert thin["plan"]["revision"] == service._plan_row(db, plan.id)["revision"]
        assert thin["current_task"]["id"] == nodes["running"].id
        assert len(lines[0].encode()) <= status_context.CHECKPOINT_BYTES + 64

        _next_input(db, "other", "turn-2")
        assert all("Current plan status" not in d for d in _digests(db, "other"))
    finally:
        db.close()


def test_the_status_commits_with_the_write_that_produced_it():
    """One durable fact: the write and the status it leaves commit together,
    journaled unattributed so the owner reads it, once per revision."""
    db = _db()
    try:
        plan, nodes = _plan(db)
        before = db.execute(
            "SELECT COUNT(*) FROM context_change_journal WHERE object_id=? AND change_kind=?",
            (plan.id, status_context.CHANGE_KIND),
        ).fetchone()[0]
        service.transition_node(nodes["pending"][1].id, "active", actor_id="worker", db=db)
        after = db.execute(
            "SELECT COUNT(*) FROM context_change_journal WHERE object_id=? AND change_kind=?",
            (plan.id, status_context.CHANGE_KIND),
        ).fetchone()[0]
        assert after == before + 1
        row = db.execute(
            "SELECT actor_thread_id,change_kind FROM context_change_journal WHERE object_id=? ORDER BY seq DESC LIMIT 1",
            (plan.id,),
        ).fetchone()
        assert (row["actor_thread_id"], row["change_kind"]) == (None, status_context.CHANGE_KIND)
    finally:
        db.close()
