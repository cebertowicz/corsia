"""Provider refusals replace generic liveness retries with one reset deadline."""

from __future__ import annotations

from automation.turn_end_effects import PendingTurnEndEffects
from automation import plan_liveness_terminal
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from automation import plan_liveness as pl
from automation.continuation.sources.liveness import LivenessSource
from infra.database import get_db
from infra.input_admission import accept_thread_input
from infra.thread_driver import delete_thread_turns, finish_turn
from infra.thread_inputs import start_idle_turn
from plans import boundary, operator_projection, service, status
from tests.event_routing_helpers import mk_session


_RESET = "2099-01-01T12:00:00+00:00"
_BEFORE_RESET = "2099-01-01T11:59:59+00:00"
_AFTER_RESET = "2099-01-01T12:00:01+00:00"
_PROVIDER_ERROR = "You've hit your session limit. Resets at 4:20 PM."


def _start_selected_turn(thread_id: str, turn_id: str):
    """Create one admitted Turn with immutable current plan selection."""
    db = get_db()
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title="Continue the selected work",
        acceptance_criteria="The provider can continue this task",
        db=db,
    )
    boundary.mutate(
        plan.id,
        "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        actor=boundary.PlanActor(thread_id, "user-1", None),
        provenance="test.provider_reset",
        mutation_id=f"activate-{turn_id}",
        db=db,
    )
    accept_thread_input(
        thread_id,
        role="user",
        content="continue",
        intent="apply_now",
        message_id=f"input-{turn_id}",
        db=db,
    )
    batch = start_idle_turn(
        thread_id,
        turn_id=turn_id,
        boot_epoch="provider-reset-test",
        started_at="2099-01-01T11:00:00+00:00",
        db=db,
    )
    assert batch is not None
    return db, plan, node


def _terminal_error(thread_id: str, turn_id: str, *, error: str, clears_at: str | None = None):
    """Finish a real admitted Turn, then call the canonical plan terminal seam."""
    db = get_db()
    finish_turn(
        turn_id,
        outcome="error",
        ended_at="2099-01-01T11:01:00+00:00",
        successor_turn_id=f"{turn_id}-next",
        boot_epoch="provider-reset-test",
        error=error,
        db=db,
    )
    return plan_liveness_terminal.on_thread_turn_complete(
        thread_id,
        outcome="error",
        error=error,
        turn_id=turn_id,
        clears_at=clears_at,
        db=db,
        effects=PendingTurnEndEffects(),
    )


def _wake(thread_id: str):
    return get_db().execute(
        "SELECT * FROM plan_liveness_wakes WHERE thread_id=?", (thread_id,)
    ).fetchone()


@pytest.mark.asyncio
async def test_known_provider_reset_waits_until_due_then_delivers_once(isolated_db):
    thread_id = mk_session("provider-reset-known")
    db, plan, node = _start_selected_turn(thread_id, "known-turn")

    _terminal_error(
        thread_id,
        "known-turn",
        error=_PROVIDER_ERROR,
        clears_at=_RESET,
    )
    wake = _wake(thread_id)
    assert wake["deadline"] == _RESET
    assert wake["provider_reset_at"] == _RESET
    assert wake["provider_reset_turn_id"] == "known-turn"
    actor = boundary.PlanActor(None, "user-1", None, kind="user")
    full = operator_projection.read_thread(thread_id, actor=actor, db=db)
    projected = full["plans"][0]
    shown = {item["id"]: item for item in projected["nodes"]}[node.id]
    assert shown["execution"]["condition"] == "running"
    assert shown["display"]["label_key"] == "waiting"
    assert shown["status_reason"] == "Provider resets at 2099-01-01T12:00:00+00:00"
    assert projected["progress"]["distribution"]["waiting"] == 1
    assert projected["progress"]["distribution"]["pending_ready"] == 0
    assert projected["display"]["label_key"] == "waiting"
    compact = status.read(db, service._plan_row(db, plan.id), actor=actor)
    current = {item["id"]: item for item in compact["current"]["rows"]}[node.id]
    assert current["condition"] == "waiting"
    assert current["label"] == "waiting"
    assert current["reason"] == "Provider resets at 2099-01-01T12:00:00+00:00"

    early: list[tuple] = []
    result = await pl.sweep_plan_wakes(
        _BEFORE_RESET,
        db=get_db(),
        deliver=lambda *args: early.append(args),
    )
    assert result.get("woken", []) == []
    assert early == []

    delivered: list[tuple] = []

    async def deliver(*args):
        delivered.append(args)
        return True

    result = await pl.sweep_plan_wakes(
        _AFTER_RESET,
        db=get_db(),
        deliver=deliver,
    )
    assert result["woken"] == [thread_id]
    assert len(delivered) == 1
    assert delivered[0][2] == (
        "The provider's reported reset time is due. Re-read your plan, then "
        "resume the selected work if the provider is available."
    )
    assert delivered[0][3]["provider_reset_at"] == _RESET
    after_due = dict(_wake(thread_id))
    assert after_due["provider_reset_at"] is None
    assert after_due["provider_reset_turn_id"] == "known-turn"
    plan_liveness_terminal.on_thread_turn_complete(
        thread_id, outcome="error", error=_PROVIDER_ERROR,
        turn_id="known-turn", clears_at=_RESET, db=get_db(),
        effects=PendingTurnEndEffects(),
    )
    assert dict(_wake(thread_id)) == after_due


@pytest.mark.parametrize(
    ("reset_at", "utc_reset"),
    [
        ("2099-01-01T15:00:00-04:00", "2099-01-01T19:00:00+00:00"),
        ("2099-01-02T04:00:00+09:00", "2099-01-01T19:00:00+00:00"),
    ],
)
def test_reset_deadline_uses_utc_for_due_query(isolated_db, reset_at, utc_reset):
    thread_id = mk_session(f"provider-reset-offset-{reset_at[-6:]}")
    _start_selected_turn(thread_id, "offset-turn")
    _terminal_error(
        thread_id, "offset-turn", error=_PROVIDER_ERROR, clears_at=reset_at,
    )
    assert _wake(thread_id)["deadline"] == utc_reset
    assert pl.due_plan_wakes("2099-01-01T18:59:59+00:00", db=get_db()) == []
    assert len(pl.due_plan_wakes(utc_reset, db=get_db())) == 1


@pytest.mark.parametrize("transition", ["cancelled", "stalled"])
def test_terminal_reset_wait_rearms_generic_liveness(
    isolated_db, transition,
):
    thread_id = mk_session(f"provider-reset-{transition}")
    _start_selected_turn(thread_id, f"{transition}-turn")
    _terminal_error(
        thread_id, f"{transition}-turn", error=_PROVIDER_ERROR, clears_at=_RESET,
    )
    db = get_db()
    original = pl.row_to_wake(dict(_wake(thread_id)))
    if transition == "cancelled":
        assert pl.cancel_plan_wake(thread_id, db=db)
    else:
        assert pl._claim_fire(original.id, pl.STALLED, db)
    assert _wake(thread_id)["provider_reset_at"] is None
    from plans.projections import liveness_signal

    signal = liveness_signal(thread_id, db=db)
    if transition == "stalled":
        from dataclasses import replace

        signal = replace(signal, settled_events=signal.settled_events + 1)
    rearmed = pl.arm_plan_wake(thread_id, signal, db=db)
    assert rearmed is not None and rearmed.status == pl.ARMED
    assert rearmed.provider_reset_at is None


@pytest.mark.parametrize("state", ["queued", "running", "stale"])
def test_provider_reset_wake_is_stale_when_the_thread_already_starts_work(isolated_db, state):
    thread_id = mk_session(f"provider-reset-{state}-work")
    _start_selected_turn(thread_id, f"{state}-failed")
    _terminal_error(
        thread_id, f"{state}-failed", error=_PROVIDER_ERROR, clears_at=_RESET,
    )
    db = get_db()
    accept_thread_input(
        thread_id, role="user", content="continue", intent="apply_now",
        message_id=f"{state}-next", db=db,
    )
    if state == "running":
        batch = start_idle_turn(
            thread_id, turn_id=f"{state}-next-turn", boot_epoch="provider-reset-test",
            started_at="2099-01-01T11:30:00+00:00", db=db,
        )
        assert batch is not None
    if state == "stale":
        db.execute(
            "UPDATE messages SET target_execution_epoch=target_execution_epoch+1 WHERE id=?",
            (f"{state}-next",),
        )
    request = LivenessSource().due(_AFTER_RESET, db=db)[0]

    assert LivenessSource().is_current(
        thread_id, request.subject_id, request.transition, request.details, db=db,
    ) is (state == "stale")


def test_collected_generic_trigger_cannot_wake_before_new_provider_reset(isolated_db):
    thread_id = mk_session("provider-reset-collected")
    _start_selected_turn(thread_id, "collected-turn")
    db = get_db()
    assert pl.on_turn_complete(thread_id, db=db) is not None
    db.execute(
        "UPDATE plan_liveness_wakes SET deadline=? WHERE thread_id=?",
        ("2000-01-01T00:00:00+00:00", thread_id),
    )
    source = LivenessSource()
    old_request = source.due("2099-01-01T11:00:00+00:00", db=db)[0]
    assert source.is_current(
        thread_id, old_request.subject_id, old_request.transition,
        old_request.details, db=db,
    )

    _terminal_error(
        thread_id, "collected-turn", error=_PROVIDER_ERROR, clears_at=_RESET,
    )
    assert _wake(thread_id)["deadline"] == _RESET
    assert not source.is_current(
        thread_id, old_request.subject_id, old_request.transition,
        old_request.details, db=db,
    )


def test_same_terminal_replay_does_not_extend_provider_reset(isolated_db, monkeypatch):
    thread_id = mk_session("provider-reset-idempotent")
    _start_selected_turn(thread_id, "same-turn")
    monkeypatch.setattr(pl, "_now", lambda: datetime(2099, 1, 1, 11, tzinfo=timezone.utc))

    _terminal_error(thread_id, "same-turn", error=_PROVIDER_ERROR, clears_at=_RESET)
    first = dict(_wake(thread_id))
    monkeypatch.setattr(pl, "_now", lambda: datetime(2099, 1, 1, 11, 30, tzinfo=timezone.utc))
    plan_liveness_terminal.on_thread_turn_complete(
        thread_id,
        outcome="error",
        error=_PROVIDER_ERROR,
        turn_id="same-turn",
        clears_at=_RESET,
        db=get_db(),
        effects=PendingTurnEndEffects(),
    )

    assert dict(_wake(thread_id)) == first
    plan_liveness_terminal.on_thread_turn_complete(
        thread_id,
        outcome="error",
        error=_PROVIDER_ERROR,
        turn_id="same-turn",
        clears_at="2099-01-01T13:00:00+00:00",
        db=get_db(),
        effects=PendingTurnEndEffects(),
    )
    assert dict(_wake(thread_id)) == first


def test_successful_alternate_model_turn_clears_old_provider_reset(isolated_db):
    thread_id = mk_session("provider-reset-alternate")
    _start_selected_turn(thread_id, "limited-turn")
    _terminal_error(thread_id, "limited-turn", error=_PROVIDER_ERROR, clears_at=_RESET)
    db = get_db()
    db.execute("UPDATE threads SET model='alternate/model' WHERE id=?", (thread_id,))
    accept_thread_input(
        thread_id,
        role="user",
        content="continue with the alternate model",
        intent="apply_now",
        message_id="alternate-input",
        db=db,
    )
    db.commit()
    assert start_idle_turn(
        thread_id,
        turn_id="alternate-turn",
        boot_epoch="provider-reset-test",
        started_at="2099-01-01T11:30:00+00:00",
        db=db,
    ) is not None
    finish_turn(
        "alternate-turn",
        outcome="completed",
        ended_at="2099-01-01T11:31:00+00:00",
        successor_turn_id="alternate-next",
        boot_epoch="provider-reset-test",
        db=db,
    )

    plan_liveness_terminal.on_thread_turn_complete(
        thread_id,
        outcome="completed",
        turn_id="alternate-turn",
        db=db,
        effects=PendingTurnEndEffects(),
    )

    wake = _wake(thread_id)
    assert wake["provider_reset_at"] is None
    assert wake["deadline"] != _RESET


def test_success_that_settled_its_own_task_still_clears_the_reset(isolated_db):
    """Debt 26: any latest success ends the wait, selected or not."""
    thread_id = mk_session("provider-reset-settled")
    _, plan, node = _start_selected_turn(thread_id, "limited-turn")
    _terminal_error(thread_id, "limited-turn", error=_PROVIDER_ERROR, clears_at=_RESET)
    db = get_db()
    service.add_node(
        plan.id, kind="task", title="Remaining work",
        acceptance_criteria="Still actionable after the first task closes", db=db,
    )
    accept_thread_input(
        thread_id, role="user", content="continue", intent="apply_now",
        message_id="settling-input", db=db,
    )
    assert start_idle_turn(
        thread_id, turn_id="settling-turn", boot_epoch="provider-reset-test",
        started_at="2099-01-01T11:30:00+00:00", db=db,
    ) is not None
    current = service.get_node(node.id, db=db)
    boundary.mutate(
        plan.id, "close_node",
        {"node_id": node.id, "expected_revision": current.revision,
         "reason": "Done another way"},
        actor=boundary.PlanActor(thread_id, "user-1", None),
        provenance="test.provider_reset", mutation_id="close-settling", db=db,
    )
    finish_turn(
        "settling-turn", outcome="completed", ended_at="2099-01-01T11:31:00+00:00",
        successor_turn_id="settling-next", boot_epoch="provider-reset-test", db=db,
    )

    plan_liveness_terminal.on_thread_turn_complete(
        thread_id, outcome="completed", turn_id="settling-turn", db=db,
        effects=PendingTurnEndEffects(),
    )

    assert _wake(thread_id)["provider_reset_at"] is None


def test_new_unknown_error_replaces_an_older_known_reset_with_generic_liveness(isolated_db):
    thread_id = mk_session("provider-reset-new-unknown")
    _start_selected_turn(thread_id, "first-turn")
    _terminal_error(thread_id, "first-turn", error=_PROVIDER_ERROR, clears_at=_RESET)
    db = get_db()
    accept_thread_input(
        thread_id, role="user", content="try another model", intent="apply_now",
        message_id="second-input", db=db,
    )
    assert start_idle_turn(
        thread_id, turn_id="second-turn", boot_epoch="provider-reset-test",
        started_at="2099-01-01T11:30:00+00:00", db=db,
    ) is not None
    finish_turn(
        "second-turn", outcome="error", ended_at="2099-01-01T11:31:00+00:00",
        successor_turn_id="second-next", boot_epoch="provider-reset-test",
        error="Provider unavailable", db=db,
    )
    plan_liveness_terminal.on_thread_turn_complete(
        thread_id, outcome="error", error="Provider unavailable",
        turn_id="second-turn", db=db, effects=PendingTurnEndEffects(),
    )
    wake = _wake(thread_id)
    assert wake["provider_reset_at"] is None
    assert wake["deadline"] != _RESET
    current = dict(wake)
    plan_liveness_terminal.on_thread_turn_complete(
        thread_id, outcome="error", error=_PROVIDER_ERROR,
        turn_id="first-turn", clears_at=_RESET, db=db,
        effects=PendingTurnEndEffects(),
    )
    assert dict(_wake(thread_id)) == current


@pytest.mark.asyncio
async def test_unknown_provider_refusal_keeps_waking_on_the_generic_backoff(isolated_db):
    thread_id = mk_session("provider-reset-unknown")
    _start_selected_turn(thread_id, "unknown-turn")

    _terminal_error(thread_id, "unknown-turn", error="Provider unavailable")
    db = get_db()
    wake = _wake(thread_id)
    assert wake["provider_reset_at"] is None
    db.execute(
        "UPDATE plan_liveness_wakes SET deadline=?,cycle=? WHERE thread_id=?",
        ("2000-01-01T00:00:00+00:00", 6, thread_id),
    )
    db.commit()

    delivered: list[tuple] = []

    async def deliver(*args):
        delivered.append(args)
        return True

    result = await pl.sweep_plan_wakes(
        _AFTER_RESET,
        db=db,
        deliver=deliver,
    )
    assert result["woken"] == [thread_id]
    assert len(delivered) == 1
    assert _wake(thread_id)["cycle"] == 7


def test_stale_terminal_without_current_selection_cannot_install_reset(isolated_db):
    thread_id = mk_session("provider-reset-stale")
    _start_selected_turn(thread_id, "current-turn")
    db = get_db()

    plan_liveness_terminal.on_thread_turn_complete(
        thread_id,
        outcome="error",
        error=_PROVIDER_ERROR,
        turn_id="missing-turn",
        clears_at=_RESET,
        db=db,
        effects=PendingTurnEndEffects(),
    )

    wake = _wake(thread_id)
    assert wake["provider_reset_at"] is None
    assert wake["provider_reset_turn_id"] is None


def test_provider_reset_marker_does_not_prevent_turn_deletion(isolated_db):
    thread_id = mk_session("provider-reset-retention")
    _start_selected_turn(thread_id, "retained-turn")
    _terminal_error(thread_id, "retained-turn", error=_PROVIDER_ERROR, clears_at=_RESET)
    db = get_db()
    delete_thread_turns(db, thread_id)
    assert db.execute(
        "SELECT COUNT(*) FROM active_turns WHERE thread_id=?", (thread_id,)
    ).fetchone()[0] == 0
    assert _wake(thread_id)["provider_reset_turn_id"] == "retained-turn"


def test_local_reset_parser_uses_host_zone_rules(monkeypatch):
    from subagents.provider_usage_limits import provider_reset_deadline

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    now = datetime(2026, 11, 1, 0, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert provider_reset_deadline(
        "You've hit your session limit. Resets at 1:30am.", now=now,
    ) is None
    assert provider_reset_deadline(
        "You've hit your session limit. Resets at 12:05am.", now=now,
    ) is not None


def test_local_reset_parser_honors_a_stated_zone_and_never_guesses_past_it():
    from subagents.provider_usage_limits import provider_reset_deadline

    host = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 9, 23, 0, 30, tzinfo=host)

    def deadline(text):
        return provider_reset_deadline(
            f"You've hit your session limit. {text}", now=now, zone=host,
        )

    # 00:30 in Los Angeles is already past 2:40 in New York and in UTC.
    assert deadline("Resets 2:40am (America/New_York)") == datetime(
        2026, 9, 24, 2, 40, tzinfo=ZoneInfo("America/New_York"),
    )
    assert deadline("Resets 2:40am UTC") == datetime(2026, 9, 24, 2, 40, tzinfo=timezone.utc)
    assert deadline("Resets 2:40am PDT") == datetime(2026, 9, 23, 2, 40, tzinfo=host)
    assert deadline("Resets 2:40am CET") is None
    assert deadline("Resets 2:40am (Mars/Olympus_Mons)") is None
    assert deadline("Resets 2:40am. Try again later.") == datetime(
        2026, 9, 23, 2, 40, tzinfo=host,
    )


def test_strict_local_reset_parser_rejects_ambiguous_and_accepts_midnight():
    zone = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 11, 1, 0, 30, tzinfo=zone)

    from subagents.provider_usage_limits import provider_reset_deadline

    assert provider_reset_deadline(
        "You've hit your session limit. Resets at 12:05am.", now=now, zone=zone
    ) is not None
    assert provider_reset_deadline(
        "You've hit your session limit. Resets at 1:30am.", now=now, zone=zone
    ) is None


def test_local_reset_parser_reads_the_vendors_dated_refusals():
    from subagents.provider_usage_limits import provider_reset_deadline

    host = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 9, 30, 2, 0, tzinfo=host)

    def deadline(text):
        return provider_reset_deadline(text, now=now, zone=host)

    assert deadline("You've hit your weekly limit · resets Sep 30 at 10pm") == datetime(
        2026, 9, 30, 22, 0, tzinfo=host,
    )
    assert deadline("You've hit your weekly limit · resets Oct 2, 10:30am") == datetime(
        2026, 10, 2, 10, 30, tzinfo=host,
    )
    assert deadline(
        "You've hit your usage limit. Upgrade to Pro, or try again at Oct 1st, 2026 2:32 AM.",
    ) == datetime(2026, 10, 1, 2, 32, tzinfo=host)
    # A dated reset already behind now is unknown, never rolled to tomorrow.
    assert deadline("You've hit your weekly limit · resets Sep 29 at 10pm") is None
    assert deadline("You've hit your weekly limit · resets Feb 30 at 10pm") is None
    assert deadline("You've hit your weekly limit · resets Foo 3 at 10pm") is None
