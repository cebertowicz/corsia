"""Each event gives the transcript chip one short line of its own (#2061).

An event's ``summary`` used to be both the text the agent reads and the label
the chip shows. The chip now has its own line, and the agent's input is the
same bytes it always was.
"""

from __future__ import annotations

import json

from prompts import plan_wakes
from tests.test_passive_changes import _db


def _digest(db, thread_id: str = "thread"):
    from infra.passive_changes import materialize_pending_digest

    accepted = materialize_pending_digest(db, thread_id)
    assert accepted is not None
    row = db.execute(
        "SELECT content,metadata FROM messages WHERE id=?", (accepted.message_id,),
    ).fetchone()
    return row["content"], json.loads(row["metadata"])


def test_a_digest_event_carries_its_chip_line_beside_the_agents_text(monkeypatch):
    from infra.passive_changes import append_change

    monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
    db = _db()
    try:
        append_change(
            db, object_kind="artifact", object_id="artifact", change_kind="updated",
            summary="Stop working on it: you were replaced.",
            details={"path": "a", "chip": "Step handed to another worker"},
        )
        append_change(
            db, object_kind="artifact", object_id="artifact", change_kind="updated",
            summary="Current plan status: {}", details={"path": "b", "chip": None},
        )
        append_change(
            db, object_kind="artifact", object_id="artifact", change_kind="updated",
            summary="Project context updated", details={"path": "c"},
        )

        content, metadata = _digest(db)

        # The agent reads exactly the journal summaries, chip lines never among them.
        assert content == (
            "[PASSIVE CONTEXT DIGEST]\n3 context updates\n"
            "- Stop working on it: you were replaced.\n"
            "- Current plan status: {}\n"
            "- Project context updated"
        )
        events = metadata["context_events"]
        assert [event.get("chip", "<absent>") for event in events] == [
            "Step handed to another worker", None, "<absent>",
        ]
        assert [event["summary"] for event in events][0] == (
            "Stop working on it: you were replaced."
        )
        assert all("chip" not in event.get("details", {}) for event in events)
    finally:
        db.close()


def test_the_per_turn_plan_status_is_agent_only_state_with_no_chip_row():
    from plans import service, status_context
    from tests.test_plan_checkpoint_projection import _digests, _next_input
    from tests.test_plan_status import _db as plan_db, _plan

    db = plan_db()
    try:
        plan, nodes = _plan(db)
        service.transition_node(nodes["pending"][0].id, "active", actor_id="worker", db=db)
        db.commit()

        journaled = db.execute(
            "SELECT summary,details_json FROM context_change_journal "
            "WHERE object_id=? AND change_kind=? ORDER BY seq DESC LIMIT 1",
            (plan.id, status_context.CHANGE_KIND),
        ).fetchone()
        assert journaled["summary"].startswith("Current plan status: {")
        assert json.loads(journaled["details_json"])["chip"] is None

        _next_input(db, "worker", "turn-1")
        (content,) = _digests(db, "worker")
        assert "Current plan status: " in content
        row = db.execute(
            "SELECT metadata FROM messages WHERE thread_id='worker' "
            "AND json_extract(metadata,'$.event_type')='context.digest'"
        ).fetchone()
        (status_event,) = [
            event for event in json.loads(row["metadata"])["context_events"]
            if event.get("change_kind") == status_context.CHANGE_KIND
        ]
        assert status_event["chip"] is None
        assert status_event["summary"].startswith("Current plan status: {")
    finally:
        db.close()


def test_every_plan_wake_has_a_chip_line_a_person_can_read():
    kinds = {
        f"plan.{name.removeprefix('WAKE_').lower()}"
        for name in dir(plan_wakes)
        if name.startswith("WAKE_") and name not in {"WAKE_CHIPS", "WAKE_STRANDED_GRANDCHILDREN"}
    }

    assert kinds <= set(plan_wakes.WAKE_CHIPS), kinds - set(plan_wakes.WAKE_CHIPS)
    assert "plan.decision_answered" in plan_wakes.WAKE_CHIPS
    for event_type, line in plan_wakes.WAKE_CHIPS.items():
        assert line and len(line) <= 60, event_type
        assert not line.startswith("["), event_type
        assert not any(mark in line for mark in ("{", "}", "delegate_status", "plan_read")), event_type


def test_a_plan_wake_row_shows_its_chip_line_and_the_agent_keeps_its_own_text():
    from tests.test_plan_wake import _db as wake_db, _submitted, _task, _update, _revision, HUMAN
    from plans import service

    db = wake_db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        reviewed = _task(db, plan.id, "Draft the brief")
        _submitted(db, plan.id, reviewed)
        _update(db, plan.id, "reject", {
            "node_id": reviewed.id, "feedback": "Needs the failure modes",
            "expected_revision": _revision(db, reviewed.id),
        }, actor=HUMAN)
        db.commit()

        row = db.execute(
            "SELECT content,metadata FROM messages WHERE id LIKE 'plan-wake:%' ORDER BY seq"
        ).fetchone()

        metadata = json.loads(row["metadata"])
        assert metadata["summary"] == plan_wakes.WAKE_CHIPS["plan.revision_requested"]
        assert row["content"].startswith(
            plan_wakes.WAKE_REVISION_REQUESTED.format(title="Draft the brief") + "\n"
        )
    finally:
        db.close()


def test_an_event_wake_chip_can_differ_from_the_summary_the_agent_reads():
    from events.delivery import enqueue_event_wake
    from tests.test_passive_changes import _db as passive_db

    db = passive_db()
    try:
        common = dict(message_id="wake", db=db)
        enqueue_event_wake(
            "thread", "webhook.github.check_suite_completed",
            "webhook.github.check_suite_completed on o/r#9 (success)", {"repo": "o/r"},
            **common,
        )
        enqueue_event_wake(
            "thread", "webhook.github.check_suite_completed",
            "webhook.github.check_suite_completed on o/r#9 (success)", {"repo": "o/r"},
            chip="CI success on o/r#9", message_id="wake-with-chip", db=db,
        )

        plain, chipped = (
            db.execute("SELECT content,metadata FROM messages WHERE id=?", (message_id,)).fetchone()
            for message_id in ("wake", "wake-with-chip")
        )
        assert chipped["content"] == plain["content"]
        assert json.loads(plain["metadata"])["summary"].startswith("webhook.github.")
        assert json.loads(chipped["metadata"])["summary"] == "CI success on o/r#9"
    finally:
        db.close()


def test_a_check_suite_chip_says_the_outcome_in_words():
    from events.router import _chip_line, _summarize

    payload = {
        "repository": {"full_name": "o/r"},
        "check_suite": {"conclusion": "failure", "id": 1, "head_sha": "abc"},
    }
    event = "webhook.github.check_suite_completed"
    summary, _details = _summarize(event, payload, "o/r#9")

    # The agent's line is unchanged; the chip says the outcome in words.
    assert summary == f"{event} on o/r#9 (failure)"
    assert _chip_line(payload, "o/r#9", summary) == "CI failure on o/r#9"
    # Every other GitHub summary is already a sentence, so the chip keeps it.
    pr = {"pull_request": {"title": "Fix it"}, "action": "opened"}
    pr_summary, _ = _summarize("webhook.github.pr_opened", pr, "o/r#9")
    assert _chip_line(pr, "o/r#9", pr_summary) == pr_summary


def test_an_artifact_change_carries_the_chip_line_to_subscribers_digest(monkeypatch):
    from registry import models

    monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
    db = _db()
    try:
        agent_line = "webhook.github.check_suite_completed on o/r#9 (failure)"
        models.record_artifact_change(
            artifact_id="artifact", internal_event="webhook.github.check_suite_completed",
            external_id="o/r#9", summary=agent_line, provenance={},
            chip="CI failure on o/r#9", db=db,
        )

        content, metadata = _digest(db)

        assert agent_line in content and "CI failure" not in content
        (event,) = metadata["context_events"]
        assert (event["summary"], event["chip"]) == (agent_line, "CI failure on o/r#9")
    finally:
        db.close()


def test_a_roster_change_shows_its_journaled_line_while_the_agent_reads_the_map():
    from infra.passive_changes import build_digest, append_agent_roster_transition, agent_roster_scope_id

    db = _db()
    try:
        append_agent_roster_transition(
            db, before=None,
            after={
                "id": "other", "user_id": "user", "project_id": "project",
                "agent_enabled": 1, "agent_status": "active", "agent_responsibility": "Owns X",
            },
            summary="Agent responsibility map updated: Other",
        )
        rows = db.execute("SELECT * FROM context_change_journal ORDER BY seq DESC").fetchall()
        assert rows[0]["object_id"] == agent_roster_scope_id("user", "project")

        content, (event,) = build_digest(
            rows, overflow=False, clock=None, roster_change=rows[0],
            roster_content="[Agent responsibility map]\n- Other: Owns X",
        )

        assert event["summary"] == "[Agent responsibility map]\n- Other: Owns X"
        assert event["chip"] == "Agent responsibility map updated: Other"
        assert "Agent responsibility map updated: Other" not in content
    finally:
        db.close()


def test_a_replaced_worker_reads_its_notice_while_the_chip_says_what_happened():
    """#2071: the replaced worker's digest keeps its text; the chip reads for a person."""
    from tests.child_parent_notice_helpers import delegated, notices, seeded_db
    from tests.plan_replace_worker_helpers import replace, started_replacement

    db = seeded_db()
    try:
        step, _plan, node = delegated(db)
        service_active(db, node.id)
        started_replacement(db)

        replace(db, step.id, "replacement")

        (notice,) = [row for row in notices(db, "worker")
                     if row["change_kind"] == "plan.worker_replaced"]
        assert notice["summary"] == plan_wakes.WORKER_REPLACED.format(
            step="Delegate work", replacement="replacement",
        )
        details = json.loads(notice["details_json"])
        assert details["chip"] == plan_wakes.WORKER_REPLACED_CHIP
    finally:
        db.close()


def test_a_drafted_decision_receipt_keeps_the_agents_text_and_shows_a_chip_line():
    """#2071: the child's own receipt shows the chip; the agent reads the content."""
    from tests.child_parent_notice_helpers import delegated, seeded_db, transcript
    from tests.test_child_parent_notice_conditions import _drafted

    db = seeded_db()
    try:
        _step, plan, node = delegated(db)

        _drafted(db, node, plan)

        (receipt,) = [row for row in transcript(db, "worker")
                      if json.loads(row["metadata"])["event_type"]
                      == "plan.decision_drafted_pending_parent"]
        assert receipt["content"] == (
            "Decision drafted: Pick the storage shape — with your parent now;"
            " pick up your next task"
        )
        metadata = json.loads(receipt["metadata"])
        assert metadata["summary"] == plan_wakes.DECISION_DRAFTED_CHIP
        assert "chip" not in metadata
    finally:
        db.close()


def service_active(db, node_id: str) -> None:
    from plans import service

    service.transition_node(node_id, "active", actor_id="worker", db=db)


def test_roster_events_ride_one_map_and_the_digest_summary_names_the_change():
    from infra.passive_changes import build_digest
    from infra.passive_digest import digest_summary

    rows = [
        {
            "seq": seq, "object_kind": "agent_roster", "object_id": f"scope-{seq}",
            "change_kind": "updated", "summary": f"Agent responsibility map updated: {seq}",
            "created_at": "t", "details_json": None,
        }
        for seq in (1, 2)
    ]
    roster = "[Agent responsibility map]\n- Owner: Owns X"
    content, events = build_digest(
        rows, overflow=False, clock=None, roster_change=rows[0], roster_content=roster,
    )
    assert content.count("[Agent responsibility map]") == 1
    assert "\n  - Owner: Owns X" in content
    assert digest_summary(content, events[:1]) == "Agent responsibility map updated: 1"


def test_a_lone_non_roster_event_keeps_its_summary_line_as_the_digest_summary():
    from infra.passive_changes import build_digest
    from infra.passive_digest import digest_summary

    rows = [{
        "seq": 1, "object_kind": "plan", "object_id": "p", "change_kind": "updated",
        "summary": "Decision drafted for the plan", "created_at": "t",
        "details_json": '{"chip": "Decision drafted"}',
    }]
    content, events = build_digest(rows, overflow=False, clock=None)
    assert events[0]["chip"] == "Decision drafted"
    assert digest_summary(content, events) == "Decision drafted for the plan"


def test_the_intent_chips_use_the_copy_glossary():
    assert plan_wakes.WAKE_CHIPS["plan.intent_revised"] == "A task’s goal changed"
    assert plan_wakes.WAKE_CHIPS["plan.intent_input"] == "What you said may affect a running task"
