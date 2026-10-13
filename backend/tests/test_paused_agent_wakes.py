"""Pause contract rules 2 and 3 (#1971): what may start a paused agent's Turn.

People reach a paused agent, and so does a scheduled wake from a schedule the
user asked for (#2286). A connected system's event, a worker's completion and
every other scheduled wake start no Turn: each ends as a cancelled row that
says why, plus one line in the digest the agent reads with its next Turn.
"""

from __future__ import annotations

import pytest

THREAD = "main-1"


@pytest.fixture
def agent(main_db):
    import plans.boundary as boundary
    import plans.service as service
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn
    from plans.boundary import PlanActor

    owner = main_db.execute("SELECT user_id FROM threads WHERE id=?", (THREAD,)).fetchone()[0]
    user = PlanActor(None, owner, None, kind="user")

    class Agent:
        def change(self, operation: str) -> None:
            plan = service.ensure_plan("thread", THREAD, db=main_db)
            boundary.mutate(
                plan.id, operation,
                {"expected_revision": service._plan_row(main_db, plan.id)["revision"]},
                actor=user, provenance="test.paused_wakes", mutation_id=operation,
                db=main_db,
            )

        def queue(self, message_id: str, *, role: str = "system", content=None,
                  metadata=None) -> None:
            accept_thread_input(THREAD, role=role, content=content or message_id,
                                intent="apply_now",
                                message_id=message_id, metadata=metadata, db=main_db)

        def event(self, message_id: str, summary: str = "PR merged") -> None:
            from events.delivery import enqueue_event_wake

            enqueue_event_wake(THREAD, "webhook.github.pr_merged", summary,
                               message_id=message_id, db=main_db)

        def start(self):
            batch = start_idle_turn(THREAD, turn_id="turn-1", boot_epoch="test",
                                    started_at="t", db=main_db)
            return None if batch is None else batch.items

        def say(self, message_id: str):
            from infra.thread_driver import admit_input

            main_db.commit()
            return admit_input(THREAD, role="user", content="hello", intent="apply_now",
                               message_id=message_id, turn_id="turn-1", boot_epoch="test",
                               admitted_at="t", db=main_db).batch.items

        def ending(self, message_id: str) -> tuple[str, str | None]:
            row = main_db.execute(
                "SELECT delivery_state, delivery_error FROM messages WHERE id=?",
                (message_id,),
            ).fetchone()
            return row[0], row[1]

    return Agent()


def _named(items) -> list[str]:
    """The inputs a test queued, without the digest that rides along."""
    return [item.message_id for item in items
            if not item.message_id.startswith("context-digest:")]


def _digest(items) -> str:
    return "\n".join(item.content for item in items
                     if item.message_id.startswith("context-digest:"))


def test_events_land_in_the_digest_the_next_person_started_turn_reads(agent) -> None:
    agent.change("pause")
    agent.event("merged", "PR #1 merged")
    assert agent.start() is None
    agent.event("verdict", "PR #1 reviewer verdict: clean")
    assert agent.start() is None

    assert agent.ending("merged") == ("cancelled", "held_while_paused")
    assert agent.ending("verdict") == ("cancelled", "held_while_paused")
    items = agent.say("hello")
    assert _named(items) == ["hello"]
    assert "Arrived while paused: PR #1 merged" in _digest(items)
    assert "Arrived while paused: PR #1 reviewer verdict: clean" in _digest(items)


def test_events_held_together_stay_one_line_each(agent) -> None:
    agent.change("pause")
    agent.event("merged", "PR #1 merged")
    agent.event("verdict", "PR #1 reviewer verdict: clean")
    assert agent.start() is None

    digest = _digest(agent.say("hello"))
    assert "Arrived while paused: PR #1 merged" in digest
    assert "Arrived while paused: PR #1 reviewer verdict: clean" in digest


def test_a_scheduled_wake_lands_in_the_digest(agent) -> None:
    agent.change("pause")
    agent.queue("nightly", metadata={"event_type": "automation.scheduled_wake",
                                     "summary": "Nightly run"})

    assert agent.start() is None
    assert agent.ending("nightly") == ("cancelled", "held_while_paused")
    assert "Arrived while paused: Nightly run" in _digest(agent.say("hello"))


def test_a_user_asked_scheduled_wake_reaches_a_paused_agent(agent, main_db) -> None:
    main_db.execute(
        "INSERT INTO automation_schedules "
        "(id,thread_id,scope_id,kind,spec,next_run_at,max_runs,state,created_by_actor,"
        "asked_by_user,created_at,updated_at) VALUES "
        "('nightly','main-1','scope','clock','2030-01-01T00:00:00+00:00',"
        "'2030-01-01T00:00:00+00:00',1,'active','main-1',1,'t','t')"
    )
    main_db.commit()
    agent.change("pause")
    agent.queue("nightly-wake", metadata={
        "event_type": "automation.scheduled_wake", "subject_id": "nightly",
    })

    assert _named(agent.start()) == ["nightly-wake"]


def test_only_a_user_asked_wake_is_stamped_with_the_stop_clause_at_claim(agent, main_db) -> None:
    import json

    from core.message_history import PAUSED_SCHEDULE_KEY, render_agent_envelope

    for schedule_id, asked in (("asked", 1), ("own", 0)):
        main_db.execute(
            "INSERT INTO automation_schedules "
            "(id,thread_id,scope_id,kind,spec,next_run_at,max_runs,state,created_by_actor,"
            "asked_by_user,created_at,updated_at) VALUES "
            "(?,'main-1','scope','clock','2030-01-01T00:00:00+00:00',"
            "'2030-01-01T00:00:00+00:00',1,'active','main-1',?,'t','t')",
            (schedule_id, asked),
        )
    main_db.commit()
    agent.change("pause")
    agent.queue("asked-wake", metadata={"event_type": "automation.scheduled_wake",
                                        "subject_id": "asked"})
    agent.queue("own-wake", metadata={"event_type": "automation.scheduled_wake",
                                      "subject_id": "own"})
    agent.queue("hello", role="user")

    assert set(_named(agent.start())) == {"asked-wake", "own-wake", "hello"}
    stored = {
        row[0]: json.loads(row[1])
        for row in main_db.execute(
            "SELECT id, metadata FROM messages WHERE id IN ('asked-wake','own-wake')"
        )
    }
    assert stored["asked-wake"][PAUSED_SCHEDULE_KEY] is True
    assert PAUSED_SCHEDULE_KEY not in stored["own-wake"]
    stop = "Do this schedule's task only, then stop."
    assert stop in render_agent_envelope("x", stored["asked-wake"]).managed_prefix
    assert stop not in render_agent_envelope("x", stored["own-wake"]).managed_prefix


@pytest.mark.parametrize("content", [
    "Subagent finished:\n\nTriage done: 4 issues closed.\nDetails follow.",
    "Triage done: 4 issues closed.\nDetails follow.",
], ids=["turn_end", "explicit_delegate_send"])
def test_a_held_worker_completion_names_the_worker_and_its_result(
    agent, main_db, content,
) -> None:
    main_db.execute(
        "INSERT INTO threads (id, user_id, title, model, created_at, updated_at) "
        "VALUES ('worker-1', 'steve', 'Triage worker', 'claude-code/sonnet', 't', 't')"
    )
    agent.change("pause")
    # The shapes `thread_driver._parent_completion` and `delegate_send` queue.
    agent.queue("done", role="user", content=content,
                metadata={"sender_kind": "agent", "from_thread": "worker-1",
                          "completion": True, "child_turn_id": "w-turn",
                          "terminal_status": "completed"})

    assert agent.start() is None
    assert agent.ending("done") == ("cancelled", "held_while_paused")
    assert ("Arrived while paused: worker Triage worker finished: Triage done: 4 issues"
            " closed. (whole result: delegate_status(thread_id='worker-1'))"
            in _digest(agent.say("hello")))


@pytest.mark.parametrize(("role", "metadata"), [
    ("user", None),
    ("user", {"sender_kind": "agent", "sender_thread_id": "peer"}),
    ("system", {"event_type": "plan.decision_answered", "origin": "plan"}),
    ("system", {"restart_registration_id": "restart-1"}),
], ids=["user", "peer", "decision_answer", "restart"])
def test_people_reach_a_paused_agent(agent, role, metadata) -> None:
    agent.change("pause")
    agent.queue("reach", role=role, metadata=metadata)

    assert _named(agent.start()) == ["reach"]


def test_a_running_agent_starts_on_an_event(agent) -> None:
    agent.change("pause")
    agent.change("resume")
    agent.event("merged")

    assert _named(agent.start()) == ["merged"]
    assert agent.ending("merged")[1] is None
