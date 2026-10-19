"""A recorded correction asks its agent to triage, on the passive context channel.

The note is journaled with the correction fact, rides the next admitted input's
digest, never starts a turn, and is offered again only when the digest that
carried it provably never reached the model.
"""

from __future__ import annotations

import json
import time

import pytest

THREAD = "main-1"
CORRECTION = {"label": "correction", "truncated": False, "original_tokens": 9, "failure_reason": "none"}
NOT_CORRECTION = {**CORRECTION, "label": "not_correction"}
NOTE_DETAILS = {
    "chip": None, "kind": "correction_note", "note_key": "message:c1",
    "correction_subjects": ["message:c1"], "reoffer_unwritten": True,
}


def _version(release: str = "v1"):
    from core.corrections_recording import ClassifierVersion

    return ClassifierVersion("corrections-classifier", release, "correction", 0.5, "modernbert-owner-context-v2-2048")


@pytest.fixture
def agent(main_db, monkeypatch):
    import plans.boundary as boundary
    import plans.service as service
    from infra.thread_driver import admit_input, finish_turn
    from plans.boundary import PlanActor

    monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
    owner = main_db.execute("SELECT user_id FROM threads WHERE id=?", (THREAD,)).fetchone()[0]
    user = PlanActor(None, owner, None, kind="user")

    class Agent:
        turns = 0
        batch = None

        def change(self, operation: str) -> None:
            plan = service.ensure_plan("thread", THREAD, db=main_db)
            boundary.mutate(
                plan.id, operation,
                {"expected_revision": service._plan_row(main_db, plan.id)["revision"]},
                actor=user, provenance="test.correction_notes", mutation_id=operation,
                db=main_db,
            )

        def _claimed(self, batch):
            if batch is not None:
                self.batch = batch
            return None if batch is None else batch.items

        def say(self, message_id: str, content: str = "Thanks.", at: str = "2026-10-16T20:00:00Z"):
            """The person's message; returns the claimed items, or None while a turn runs."""
            self.turns += 1
            main_db.commit()
            admission = admit_input(
                THREAD, role="user", content=content, intent="apply_now", message_id=message_id,
                turn_id=f"turn-{self.turns}", boot_epoch="test", admitted_at=at, db=main_db,
            )
            return self._claimed(admission.batch)

        def event(self, message_id: str) -> None:
            from events.delivery import enqueue_event_wake

            enqueue_event_wake(THREAD, "webhook.github.pr_merged", "PR merged",
                               message_id=message_id, db=main_db)
            main_db.commit()

        def start(self):
            from infra.thread_inputs import start_idle_turn

            self.turns += 1
            batch = start_idle_turn(THREAD, turn_id=f"turn-{self.turns}", boot_epoch="test",
                                    started_at="t", db=main_db)
            main_db.commit()
            return self._claimed(batch)

        def record(self, message_id: str, payload=CORRECTION, version=None) -> bool:
            from jobs.corrections_recording import _record_fact, candidate_for_message

            main_db.commit()
            return _record_fact(candidate_for_message(message_id), payload, version or _version())[0]

        def write(self, outcome: str, error=None) -> None:
            """The provider write of the running turn's batch, and its outcome."""
            from infra.input_writes import begin_batch_write, settle_batch_write

            attempt = f"attempt-{self.batch.turn_id}"
            begin_batch_write(self.batch, attempt_id=attempt, started_at="t", db=main_db)
            settle_batch_write(attempt, outcome=outcome, error=error, db=main_db)
            main_db.commit()

        def deliver(self):
            """The provider accepted the batch and the turn completed; returns the successor's items."""
            self.write("accepted")
            return self.end("completed")

        def end(self, outcome: str, error=None):
            self.turns += 1
            settlement = finish_turn(
                self.batch.turn_id, outcome=outcome, ended_at="t", error=error,
                successor_turn_id=f"turn-{self.turns}", boot_epoch="test", db=main_db,
            )
            main_db.commit()
            return self._claimed(settlement.successor)

    return Agent()


def _notes(items) -> list[str]:
    """The correction subjects the batch's digests carry, in order."""
    from core.correction_notes import noted_subjects

    return [subject for item in items or () for subject in noted_subjects(item.metadata)]


def _named(items) -> list[str]:
    """The inputs a test queued, without the digest that rides along."""
    return [item.message_id for item in items or ()
            if not item.message_id.startswith("context-digest:")]


def _journal(db) -> list[dict]:
    return [json.loads(row[0]) for row in db.execute(
        "SELECT details_json FROM context_change_journal WHERE object_kind='thread_context' ORDER BY seq"
    )]


def test_the_note_text_quotes_the_person_and_names_when_they_sent_it(monkeypatch):
    from core.correction_notes import note_text

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        text = note_text("c1", "That chart uses the wrong\nunits, again.", "2026-10-16T20:05:00Z")
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert text == (
        "Correction recorded on your work (feedback c1, sent 2026-10-16 13:05 PDT): "
        '"That chart uses the wrong units, again."\n'
        "Proposed tier: none yet; you set it.\n"
        "Fix the work first. Then triage it with skill `memory-placement` section 4: "
        "set the tier, and for a Miss or Serious answer why it was missed, then watch or act. "
        "The triage is working state, not part of your reply. "
        "If you already fixed and triaged it when the message arrived, do nothing more."
    )


def test_a_fast_starting_turn_gets_the_note_on_the_next_input_never_a_turn_of_its_own(agent, main_db):
    first = agent.say("c1", "That chart uses the wrong units.")
    assert first is not None and _notes(first) == []
    assert agent.record("c1")

    assert _journal(main_db) == [NOTE_DETAILS]
    assert agent.deliver() is None  # the journaled note starts no turn
    assert main_db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id=? AND delivery_state='queued'", (THREAD,),
    ).fetchone()[0] == 0

    second = agent.say("m2")
    assert _notes(second) == ["message:c1"]
    digest = next(item for item in second if item.message_id.startswith("context-digest:"))
    assert "Correction recorded on your work (feedback c1" in digest.content
    assert digest.metadata["context_events"][0]["chip"] is None
    agent.deliver()
    assert _notes(agent.say("m3")) == []


def test_a_paused_agent_reads_the_note_once_resumed(agent, main_db):
    agent.change("pause")
    agent.say("c1", "Wrong file.")
    assert agent.record("c1")
    agent.deliver()
    # An event while paused starts no turn; the digest it built stays queued.
    agent.event("merged")
    assert agent.start() is None
    agent.change("resume")
    agent.event("verdict")

    items = agent.start()
    assert _named(items) == ["verdict"]
    assert _notes(items) == ["message:c1"]


def test_a_restart_before_the_write_offers_the_note_again_exactly_once(agent, main_db):
    agent.say("c1", "Wrong file.")
    assert agent.record("c1")
    agent.deliver()
    claimed = agent.say("m2")
    assert _notes(claimed) == ["message:c1"]

    # Startup ends a claimed, never-written turn as an error before dispatch.
    agent.end("error", RuntimeError("backend restarted"))
    digest_id = next(i.message_id for i in claimed if i.message_id.startswith("context-digest:"))
    assert tuple(main_db.execute(
        "SELECT delivery_state,write_outcome FROM messages WHERE id=?", (digest_id,),
    ).fetchone()) == ("failed", "proved_no_write")

    again = agent.say("m3")
    assert _notes(again) == ["message:c1"]
    agent.deliver()
    assert _notes(agent.say("m4")) == []


def test_a_stop_between_claim_and_write_offers_the_note_again(agent, main_db):
    agent.say("c1", "Wrong file.")
    assert agent.record("c1")
    agent.deliver()
    assert _notes(agent.say("m2")) == ["message:c1"]

    agent.end("cancelled", "stopped by user")
    assert _journal(main_db) == [NOTE_DETAILS, NOTE_DETAILS]
    assert main_db.execute(
        "SELECT actor_thread_id FROM context_change_journal ORDER BY seq DESC LIMIT 1"
    ).fetchone()[0] is None
    assert _notes(agent.say("m3")) == ["message:c1"]


def test_a_cancelled_queued_digest_offers_the_note_again(agent, main_db):
    from infra.queued_input_settlement import cancel_queued_input

    agent.say("c1", "Wrong file.")
    assert agent.record("c1")
    # The next input queues behind the running turn, its digest just before it.
    assert agent.say("m2") is None
    digest = main_db.execute(
        "SELECT id,delivery_revision FROM messages WHERE id LIKE 'context-digest:%' "
        "AND delivery_state='queued'",
    ).fetchone()
    cancel_queued_input(THREAD, digest["id"], revision=digest["delivery_revision"], db=main_db)
    main_db.commit()

    # A successor claim builds no digest, so the re-offered note waits for new input.
    successor = agent.deliver()
    assert _named(successor) == ["m2"] and _notes(successor) == []
    agent.deliver()
    assert _notes(agent.say("m3")) == ["message:c1"]


def test_an_ambiguous_write_is_never_offered_again(agent, main_db):
    agent.say("c1", "Wrong file.")
    assert agent.record("c1")
    agent.deliver()
    assert _notes(agent.say("m2")) == ["message:c1"]
    agent.write("ambiguous", "restart after dispatch")
    agent.end("error", RuntimeError("restart"))

    assert _notes(agent.say("m3")) == []


@pytest.mark.parametrize("target", ["missing", "archived", "ui"])
def test_no_target_gives_no_note(main_db, target):
    from core.corrections_recording import CorrectionCandidate
    from jobs.corrections_recording import _record_fact

    thread = {"missing": "gone", "archived": THREAD, "ui": "ui"}[target]
    if target == "archived":
        main_db.execute(
            "UPDATE threads SET lifecycle_state='archived',archived_reason='user' WHERE id=?", (THREAD,),
        )
    if target == "ui":
        main_db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "SELECT 'ui',user_id,'m','t','t' FROM threads WHERE id=?", (THREAD,),
        )
    main_db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES ('c1',?,?,?,?,?)",
        ("ui" if target == "ui" else THREAD, "user", "Wrong file.", "{}", "2026-10-16T20:00:00Z"),
    )
    main_db.commit()
    candidate = CorrectionCandidate("message", "c1", thread, None, "Wrong file.", "2026-10-16T20:00:00Z", arrived=True)

    assert _record_fact(candidate, CORRECTION, _version())[0]
    assert _journal(main_db) == []


def test_a_duplicate_or_later_version_records_but_notes_once(agent, main_db):
    agent.say("c1", "Wrong file.")
    assert agent.record("c1")
    assert not agent.record("c1")
    assert agent.record("c1", version=_version("v2"))

    assert _journal(main_db) == [NOTE_DETAILS]


def test_a_message_that_is_not_a_correction_gives_no_note(agent, main_db):
    agent.say("m1", "Looks good, ship it.")
    assert agent.record("m1", NOT_CORRECTION)
    assert _journal(main_db) == []


def _scanned(message_id: str):
    from dataclasses import replace

    from jobs.corrections_recording import candidate_for_message

    return replace(candidate_for_message(message_id), arrived=False)


def test_a_backfill_before_activation_never_notes(agent, main_db):
    from jobs.corrections_recording import _record_fact

    agent.say("c1", "Wrong file.")
    main_db.commit()

    assert _record_fact(_scanned("c1"), CORRECTION, _version())[0]
    assert _journal(main_db) == []


def test_a_scan_catch_up_notes_only_the_latest_owner_message(agent, main_db):
    from core.corrections_recording import ACTIVE_VERSION_SETTING, version_key
    from infra.settings import set_setting
    from jobs.corrections_recording import _record_fact

    agent.say("c1", "Wrong file.", at="2026-10-16T20:00:00Z")
    agent.deliver()
    agent.say("c2", "And the date is wrong.", at="2026-10-16T20:01:00Z")
    set_setting(ACTIVE_VERSION_SETTING, {"key": version_key(_version())}, db=main_db)
    main_db.commit()

    # Behind a newer owner message: no note. The latest one: one catch-up note.
    assert _record_fact(_scanned("c1"), CORRECTION, _version())[0]
    assert _journal(main_db) == []
    assert _record_fact(_scanned("c2"), CORRECTION, _version())[0]
    assert [entry["correction_subjects"] for entry in _journal(main_db)] == [["message:c2"]]


def test_a_lesson_placed_in_the_turn_the_note_arrived_is_stamped_with_it(agent, main_db):
    from core.lesson_corrections import current_owner_subjects

    agent.say("c1", "Wrong file.")
    assert agent.record("c1")
    agent.deliver()
    agent.event("merged")
    items = agent.start()
    assert _named(items) == ["merged"] and _notes(items) == ["message:c1"]

    assert current_owner_subjects(main_db, THREAD) == ("message:c1",)
