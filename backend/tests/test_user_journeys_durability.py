"""USER JOURNEYS — Part B: the same day, later.

Part A (``tests/test_user_journeys.py``) walks each journey once. This file
walks the parts of the spec that only show up the SECOND time, on the SECOND
screen, or after the machine has been away: P5 exit parity, P6 repeat, P7
sibling surfaces, P2/P8 durability across a crash.

These are separated from Part A because they are slower and because they are a
different question — not "does the journey work" but "does it keep working" —
not because they are optional. "Works the first time" is the classic miss:
edit-and-resend was reported as working only after an app restart.

The crash journey is last on purpose: it kills the module's shared backend
process and boots a new one over the same durable state.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from journey_fixtures import (  # noqa: F401 — `journeys` is used as a fixture
    GATE,
    assert_idle_clean,
    assert_thread_still_answers,
    by_content,
    journeys,
    one_turn,
    settled,
    states,
    transcript_order,
    user_rows,
)
from tests.contract.matrix_runner import TurnDriver, assistant_texts, frame_types

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

HELD = {"content": "HELD_UNTIL_RELEASED", "completion_gate": GATE,
        "finish_reason": "stop"}


async def _queue_behind_a_held_turn(
    journeys, driver: TurnDriver, thread_id: str, first: str, queued: str,
) -> dict:
    """Drive a thread to the exact state every journey below starts from.

    A turn is running and held open, and one input is durably queued behind it.
    Returns the queued row. The hold is a gate rather than a delay so "during
    the turn" is a fact rather than a bet on timing (P11).
    """
    await driver.subscribe(thread_id)
    await driver.send(thread_id, first)
    await driver.collect(thread_id, until=("turn_activity",), timeout=30)
    await driver.send(thread_id, queued, delivery_intent="queue_after_turn")
    await driver.collect(thread_id, until=("input_queued",), timeout=30)
    return by_content(await user_rows(journeys, thread_id), queued)


def _rows_seen(frames: list[dict], content: str, state: str) -> list[dict]:
    """Broadcast rows for one message in one delivery state, as a surface sees them."""
    return [
        frame["message"] for frame in frames
        if frame["type"] == "message_added"
        and frame["message"]["content"] == content
        and frame["message"]["delivery_state"] == state
    ]


async def _watch_for_row(
    driver: TurnDriver, thread_id: str, content: str, state: str,
    timeout: float = 45,
) -> list[dict]:
    """Read a surface's stream until it is told one message reached one state.

    A surface learns nothing by a single frame arriving; it learns by the row
    it is holding changing. So this waits for the ROW, not for a frame type,
    and returns everything it read on the way for the failure message.
    """
    deadline = time.monotonic() + timeout
    seen: list[dict] = []
    while time.monotonic() < deadline:
        seen += await driver.collect(
            thread_id, until=("message_added",), timeout=5,
        )
        if _rows_seen(seen, content, state):
            return seen
    return seen


# ---------------------------------------------------------------------------
# J7 — every waiting input is exitable, both ways, twice
# ---------------------------------------------------------------------------


async def test_j7_a_waiting_input_offers_both_exits_and_retry_is_idempotent(
    journeys,
) -> None:
    """J7 (P5) — a message is waiting; the user changes their mind about it.

    Every state the user can enter must be exitable BY THE USER, both exits
    must work, and pressing one twice must be harmless. A control that works
    once and then 500s is a control the user cannot trust the first time.
    """
    thread_id = await journeys.create_session("J7 exits")
    await journeys.seed_replies([HELD])

    async with TurnDriver(journeys.backend) as driver:
        target = await _queue_behind_a_held_turn(
            journeys, driver, thread_id, "the running turn", "waiting on it",
        )

        first = await journeys.message_action(thread_id, target["id"], "retry")
        again = await journeys.message_action(thread_id, target["id"], "retry")
        assert first["retried"] and again["retried"], (
            "Retry on a waiting input is not available, or works only once"
        )

        cancelled = await journeys.message_action(thread_id, target["id"], "cancel")
        assert cancelled["message"]["delivery_state"] == "cancelled"

        # PAST (P5) — the second press. It must be a definite, explained
        # refusal, not a crash and not a silent second cancellation.
        with pytest.raises(httpx.HTTPStatusError) as refused:
            await journeys.message_action(thread_id, target["id"], "cancel")
        assert refused.value.response.status_code == 409, (
            f"the second Cancel answered {refused.value.response.status_code}, "
            f"which is not a refusal a client can render"
        )

        await journeys.release_gate(GATE)
        await driver.collect(thread_id, until=("turn_complete", "error"), timeout=60)

    await assert_idle_clean(journeys, thread_id)
    await assert_thread_still_answers(journeys, thread_id, "ALIVE_AFTER_EXITS")


# ---------------------------------------------------------------------------
# J8 — do it again (P6)
# ---------------------------------------------------------------------------


async def test_j8_the_queue_journey_repeats_in_the_same_thread(journeys) -> None:
    """J8 (P6) — queue, drain, queue again, drain again, on one thread.

    First-time-only correctness is the miss this whole suite exists to catch.
    The second round must behave identically AND must not disturb the first:
    the round-one rows keep the sequences they earned.
    """
    thread_id = await journeys.create_session("J8 repeat")
    landed: list[int] = []

    for round_number in (1, 2):
        await journeys.seed_replies([
            {"content": f"HOLD_{round_number}", "completion_gate": GATE,
             "finish_reason": "stop"},
            {"content": f"DRAINED_{round_number}", "finish_reason": "stop"},
        ])
        async with TurnDriver(journeys.backend) as driver:
            await _queue_behind_a_held_turn(
                journeys, driver, thread_id,
                f"round {round_number} first", f"round {round_number} queued",
            )
            await journeys.release_gate(GATE)
            await driver.collect(thread_id, until=("turn_complete",), timeout=45)
            await driver.collect(thread_id, until=("turn_complete",), timeout=45)

        rows = await settled(journeys, thread_id, timeout=45)
        drained = by_content(rows, f"round {round_number} queued")
        assert drained["delivery_state"] == "delivered", (
            f"round {round_number} of the same journey behaved differently from "
            f"round 1: {states(rows)}"
        )
        landed.append(int(drained["seq"]))
        await assert_idle_clean(journeys, thread_id)

    assert landed[0] < landed[1], f"the second round landed above the first: {landed}"
    transcript = transcript_order(await journeys.messages(thread_id))
    assert transcript[0].startswith("[PASSIVE CONTEXT DIGEST]\nCurrent time: ")
    assert transcript[1:] == [
        "round 1 first", "HOLD_1", "round 1 queued", "DRAINED_1",
        "round 2 first", "HOLD_2", "round 2 queued", "DRAINED_2",
    ]


# ---------------------------------------------------------------------------
# J9 — the phone and the desktop are looking at the same thread [2S]
# ---------------------------------------------------------------------------


async def test_j9_a_second_surface_sees_the_queue_and_its_delivery(journeys) -> None:
    """J9 (P7) — two surfaces are attached to one thread at the same time.

    The truth is the thread's, not the sending client's. A surface that did not
    send the message must still learn that it is queued and, one step past,
    that it stopped being queued — otherwise the other device keeps a chip on
    screen for a message that is already in the transcript.
    """
    thread_id = await journeys.create_session("J9 two surfaces")
    await journeys.seed_replies([
        {"content": "HOLD_2S", "completion_gate": GATE, "finish_reason": "stop"},
        {"content": "DRAINED_2S", "finish_reason": "stop"},
    ])

    async with TurnDriver(journeys.backend) as phone, \
            TurnDriver(journeys.backend) as desktop:
        await desktop.subscribe(thread_id)
        await _queue_behind_a_held_turn(
            journeys, phone, thread_id, "sent from the phone", "queued from the phone",
        )
        # `input_queued` is the SENDER's receipt, correlated by its own
        # `input_id`; the shared truth every other surface reads is the
        # broadcast row. The desktop must therefore see the queued row itself.
        watched = await _watch_for_row(
            desktop, thread_id, "queued from the phone", "queued", timeout=30,
        )
        assert _rows_seen(watched, "queued from the phone", "queued"), (
            f"the second surface never learned a message was queued: "
            f"{frame_types(watched)}"
        )

        await journeys.release_gate(GATE)
        # PAST (P1 on the other device) — the delivery is announced to the
        # surface that did not send it, carrying the state that makes its chip
        # let go. A surface that only ever hears "queued" keeps the chip.
        seen = await _watch_for_row(
            desktop, thread_id, "queued from the phone", "delivered", timeout=60,
        )
        seen += await desktop.collect(thread_id, until=("turn_complete",), timeout=45)
        if "DRAINED_2S" not in "".join(assistant_texts(seen)):
            seen += await desktop.collect(
                thread_id, until=("turn_complete",), timeout=45,
            )

    promoted = _rows_seen(seen, "queued from the phone", "delivered")
    assert promoted, (
        "the second surface saw the queue form and never saw it drain, so its "
        f"queued chip would sit on screen after the message was delivered "
        f"({frame_types(seen)})"
    )
    assert promoted[-1]["seq"] is not None
    assert "DRAINED_2S" in "".join(assistant_texts(seen)), frame_types(seen)
    await assert_idle_clean(journeys, thread_id)


# ---------------------------------------------------------------------------
# J10 — the machine went away (crash + boot). LAST: it kills the process.
# ---------------------------------------------------------------------------


async def test_j10_a_crash_leaves_the_transcript_intact_and_offers_no_stop(
    journeys,
) -> None:
    """J10 (P2, P8, A12) — the app dies mid-turn and comes back.

    Finished work is byte-identical after boot, and the boot does not resurrect
    the turn that died. Because the process died after provider dispatch, that
    exact input becomes visibly ambiguous and is never replayed automatically.
    """
    thread_id = await journeys.create_session("J10 crash and boot")
    await journeys.seed_replies([{"content": "BEFORE_CRASH", "finish_reason": "stop"}])
    await one_turn(journeys, thread_id, "finished before the crash")
    before = await journeys.messages(thread_id)

    # The response gate holds the provider between the write and the start of
    # its response, so the crash lands after dispatch and before acceptance.
    # A delayed stream would accept on open and race the crash to 'delivered'.
    await journeys.seed_replies([{"content": "NEVER", "response_gate": "j10-never"}])
    async with TurnDriver(journeys.backend) as driver:
        await driver.subscribe(thread_id)
        await driver.send(thread_id, "in flight when the process dies")
        await driver.collect(thread_id, until=("turn_activity",), timeout=30)
        deadline = asyncio.get_running_loop().time() + 30
        while await journeys.call_count() < 1:
            assert asyncio.get_running_loop().time() < deadline, (
                "the provider was never called for the in-flight input"
            )
            await asyncio.sleep(0.05)

    journeys.backend.restart_after_crash()

    # What the user does after a crash is reopen the app. Nothing else is asked
    # of the system here: no repair call, no resend, no Retry press.
    async with TurnDriver(journeys.backend) as reopened:
        recovered, recovery_violation = await reopened.observe_recovery(
            thread_id, timeout=90,
        )
    assert recovery_violation is None, recovery_violation

    after = await journeys.messages(thread_id)
    kept = {row["id"]: (row["seq"], row["created_at"], row["delivery_state"])
            for row in after}
    for row in before:
        assert kept.get(row["id"]) == (
            row["seq"], row["created_at"], row["delivery_state"]
        ), f"the crash rewrote finished work: {states(before)} -> {states(after)}"

    assert await journeys.active_turn_count(thread_id) == 0, (
        "the boot restored a turn that died with the process, so the composer "
        "would offer Stop for a turn that can never end (A12)"
    )
    rows = await settled(journeys, thread_id, timeout=90)
    assert not [row for row in rows if row["delivery_state"] == "queued"], (
        f"an input is still waiting one boot after the crash, with nothing "
        f"left to deliver it: {states(rows)} (post-boot frames "
        f"{frame_types(recovered)})"
    )
    crashed = by_content(rows, "in flight when the process dies")
    assert crashed["delivery_state"] == "ambiguous", states(rows)
    assert crashed["seq"] is not None
    assert not [
        row for row in await journeys.messages(thread_id)
        if row["role"] == "assistant" and row["seq"] is not None
        and row["seq"] > crashed["seq"]
    ], f"the ambiguous input replayed automatically after boot: {states(rows)}"


async def test_j10_past_the_thread_answers_the_next_message_after_a_boot(
    journeys,
) -> None:
    """J10 PAST (A9) — the step after "recovery worked": USE the thread.

    Recovering the message that was in flight is half the promise. The other
    half is that the user can carry on, and carrying on means typing the next
    message and getting an answer — with no repair call, no Retry press, and
    nothing to clean up first.
    """
    thread_id = await journeys.create_session("J10 use the thread after a boot")
    # The crash must land MID-TURN, which is when recovery has work to do and
    # writes the recovery event this journey trips over. A crash on an idle
    # thread recovers cleanly and would hide the defect.
    await journeys.seed_replies([{"content": "NEVER", "delay_ms": 120_000}])
    async with TurnDriver(journeys.backend) as driver:
        await driver.subscribe(thread_id)
        await driver.send(thread_id, "in flight when the process dies")
        await driver.collect(thread_id, until=("turn_activity",), timeout=30)

    journeys.backend.restart_after_crash()

    async with TurnDriver(journeys.backend) as reopened:
        _, recovery_violation = await reopened.observe_recovery(
            thread_id, timeout=60,
        )
    assert recovery_violation is None, recovery_violation
    await settled(journeys, thread_id, timeout=60)

    await journeys.seed_replies([
        {"content": "ANSWERED_AFTER_BOOT", "finish_reason": "stop"},
    ])
    frames = await one_turn(journeys, thread_id, "the next thing I want", timeout=45)

    assert "ANSWERED_AFTER_BOOT" in "".join(assistant_texts(frames)), (
        f"the thread took the message and never answered it, and offers the "
        f"user nothing to press: {frame_types(frames)} "
        f"rows={states(await user_rows(journeys, thread_id))}"
    )


# ---------------------------------------------------------------------------
# J11 — the user changes model mid-thread and carries on
# ---------------------------------------------------------------------------


async def test_j11_a_model_switch_leaves_the_thread_usable(journeys) -> None:
    """J11 (P6, A9) — switch the model, then USE the thread.

    A switch is not a settings edit; it re-targets everything the next turn
    will do — the resolved target, the rendered context, the checkpoint that
    may have to be folded to fit the destination. The step that gets skipped is
    the one after the switch succeeds: send the next message and get an answer,
    with nothing to repair and no Stop offered for a turn nobody started.

    The prior turns stay exactly as they were: a switch re-targets what comes
    NEXT and rewrites no history.
    """
    thread_id = await journeys.create_session("J11 model switch")
    await journeys.seed_replies([{"content": "ON_THE_FIRST_MODEL",
                                  "finish_reason": "stop"}])
    await one_turn(journeys, thread_id, "before the switch")
    before = await journeys.messages(thread_id)

    switched = await journeys.switch_model(thread_id, "mock/pressure-model")
    assert switched, "the model switch returned nothing the client can render"
    assert (await journeys.session_row(thread_id))["model"] == (
        "mock/pressure-model"
    ), "the switch reported success and the thread kept its old model"

    await journeys.seed_replies([{"content": "ON_THE_SECOND_MODEL",
                                  "finish_reason": "stop"}])
    frames = await one_turn(journeys, thread_id, "after the switch", timeout=90)
    assert "ON_THE_SECOND_MODEL" in "".join(assistant_texts(frames)), (
        f"the thread took the message after a model switch and never answered "
        f"it: {frame_types(frames)} "
        f"rows={states(await user_rows(journeys, thread_id))}"
    )

    after = await journeys.messages(thread_id)
    kept = {row["id"]: (row["seq"], row["content"]) for row in after}
    for row in before:
        assert kept.get(row["id"]) == (row["seq"], row["content"]), (
            f"the model switch rewrote finished history: "
            f"{states(before)} -> {states(after)}"
        )
    await assert_idle_clean(journeys, thread_id)


async def test_j11_past_a_switch_into_a_smaller_model_never_asks(
    journeys,
) -> None:
    """J11 PAST (A9) — switch a LONG thread into a model it does not fit.

    PRODUCT RULE (owner, 2026-08-10): a switch is never a question. When the
    history does not fit the destination, the system checkpoints on the model
    being LEFT — the warm path, which already holds the cached prefix — and
    carries on. The destination writes the checkpoint only if the source
    provider refuses. This used to answer `428 checkpoint_required` and make
    the user choose, which is a prompt for a decision whose answer is the warm
    path essentially always.

    So the assertion is that ONE unattended call does the whole thing: the
    thread ends up on the new model and answers, with no second request and
    nothing for the user to press.

    ``seed_pressured_history`` selects the small model itself, so the journey
    goes up first and then back down — that up-then-down is what makes the
    second switch a real downshift with history already in place.
    """
    from tests.contract.matrix_runner import CHECKPOINT_REPLY, TOKEN

    thread_id = await journeys.create_session("J11 switch into a smaller model")
    await journeys.seed_pressured_history(thread_id)
    roomier_profile = f"/api/models/mock-test-model/profile?token={TOKEN}"
    await journeys._request(
        "PATCH", roomier_profile, json={"context_window": 393_216},
    )

    await journeys.switch_model(thread_id, "mock/test-model")
    assert (await journeys.session_row(thread_id))["model"] == "mock/test-model"
    # The successful-pressure fixture must fit its own checkpoint request.
    # Add delivered history here so this separate downshift really exceeds
    # the smaller destination even without a new foreground input.
    await journeys.seed_replies([{"content": "ROOM_TO_BREATHE\n" + "retained detail " * 2000,
                                  "finish_reason": "stop"}])
    frames = await one_turn(journeys, thread_id, "on the roomier model", timeout=120)
    assert "ROOM_TO_BREATHE" in "".join(assistant_texts(frames)), frame_types(frames)

    # ONE call, no question asked. The fold runs in the background, so the
    # answer is an accepted operation rather than a thread held open for it.
    await journeys.seed_replies([CHECKPOINT_REPLY])
    accepted = await journeys.switch_model(thread_id, "mock/pressure-model")
    assert accepted.get("operation_id"), (
        f"the switch gave the client nothing to follow: {accepted}"
    )

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if (await journeys.session_row(thread_id))["model"] == (
            "mock/pressure-model"
        ):
            break
        await asyncio.sleep(1)
    assert (await journeys.session_row(thread_id))["model"] == (
        "mock/pressure-model"
    ), f"the unattended switch never landed (accepted as {accepted})"
    assert await journeys.call_count() == 1, "downshift must checkpoint exactly once"

    await journeys.seed_replies([{"content": "ANSWERED_AFTER_THE_DOWNSHIFT",
                                  "finish_reason": "stop"}])
    frames = await one_turn(journeys, thread_id, "after the downshift", timeout=120)
    assert "ANSWERED_AFTER_THE_DOWNSHIFT" in "".join(assistant_texts(frames)), (
        f"the thread could not be used after an unattended model switch: "
        f"{frame_types(frames)} "
        f"rows={states(await user_rows(journeys, thread_id))}"
    )
    await assert_idle_clean(journeys, thread_id)
    await journeys._request(
        "PATCH", roomier_profile, json={"context_window": 128_000},
    )
