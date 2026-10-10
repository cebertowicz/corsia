"""USER JOURNEYS — Part A1, the core conversation.

Why this file exists. In two messages of casual use the product owner found
four user-visible defects, and every one of them sat ONE STEP PAST where a test
stopped: a test proved a queued chip renders and never that it disappears; a
test proved Stop ends a turn and never that a queued message survives it; a
test proved a message sends and never that a CANCELLED message lands in the
right place with a timestamp. Each unit was green. No test encoded the journey.

So each test below is a journey, not a unit. It walks the steps a person walks,
and its last assertion is the step a lazy test skips. The taxonomy those last
assertions come from (spec §0.1) is quoted in each docstring:

    P1 disappearance   P2 non-recurrence   P3 position and time
    P4 signal          P5 exit parity      P6 repeat
    P7 sibling surface P8 durable truth    P9 nothing else moved
    P10 plain language P11 race edge       P12 idle-clean at rest

Ambient invariants (spec §0.2) are asserted continuously rather than once:
``assert_idle_clean`` carries A12 and P12 after every terminal event,
``by_content`` carries A5 (nothing renders twice) at every lookup, and
``assert_thread_still_answers`` carries A9 at every exit.

WHAT THIS FILE CANNOT SEE. It drives the real backend through the real socket,
so it owns durable and protocol truth. Whether a chip leaves the screen, a
timestamp is printed, or a code reaches a human eye is a rendering question and
lives in ``frontend/src/lib/journeys/``. Steps no automation can honestly reach
— a real thumb holding the Send button, a 390px viewport, an overnight
scheduler, a second physical device — are written down in
``tests/JOURNEYS-manual.md`` instead of being pretended here.

THE SUITE IS THREE FILES, one entry point. This one walks a message from typed
to answered (J1 cold open, J2 tools, J3 queue, J6 steer). Its siblings:

* ``tests/test_user_journeys_endings.py`` — Part A2: Stop and Cancel, the two
  ways a user takes something back (J4, J5).
* ``tests/test_user_journeys_durability.py`` — Part B: the same journeys a
  second time, on a second surface, and after a crash (J7-J10).

All three share the harness in ``tests/journey_fixtures.py``, which is the
maintained isolated-backend driver from ``tests/contract/matrix_runner.py`` —
never a second one.

ANTI-VACUITY. Every journey here was proven able to fail by breaking the
behaviour in the production source and watching the cell go red (each reverted
afterwards). The five load-bearing ones, with the breakage that reddened them,
are listed in ``tests/JOURNEYS-manual.md`` under "Proven non-vacuous". A green
journey suite that cannot fail would be worse than none.
"""

from __future__ import annotations

import json

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
from tests.contract.matrix_runner import (
    TurnDriver,
    assistant_texts,
    frame_types,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

HELD = {"content": "HELD_UNTIL_RELEASED", "completion_gate": GATE,
        "finish_reason": "stop"}


# ---------------------------------------------------------------------------
# J1 — cold open: the first thread of the morning
# ---------------------------------------------------------------------------


async def test_j1_1_the_context_chip_reads_before_the_user_has_sent_anything(
    journeys,
) -> None:
    """J1.1 — open the app to a fresh thread and look at the composer chip.

    PAST (P4, A10, USER-REPORTS #4): a chip that reads 0% until the first send
    is not "not measured yet", it is a wrong number shown with confidence. The
    estimate must exist BEFORE any turn, name where its tokens came from, and
    be backed by a real inventory of sources rather than an empty list.
    """
    thread_id = await journeys.create_session("J1 cold open")

    preview = await journeys.context_preview(thread_id)

    assert 0 < float(preview["utilization"]) < 1, (
        f"the composer chip has no plausible number to show before the first "
        f"send: utilization={preview['utilization']!r}"
    )
    assert int(preview["total_used"]) > 0
    assert preview["token_source"] in ("measured", "estimated"), (
        f"the chip cannot say where its number came from: {preview['token_source']!r}"
    )
    inventory = (preview.get("context_snapshot") or {}).get("source_inventory") or []
    assert inventory, "the context sheet would open on an empty source list"


async def test_j1_3_to_1_5_one_send_lands_once_and_leaves_the_thread_idle_clean(
    journeys,
) -> None:
    """J1.3-1.5 — type one sentence, send it, watch it answer, let it finish.

    PAST (P1, P12, A5, A12): the interesting moment is AFTER the answer. The
    input must be an ordinary delivered message — no pending descriptor, no
    second copy beside the optimistic echo — and the thread must be idle-clean:
    nothing queued, no turn the composer would still offer Stop for.
    """
    thread_id = await journeys.create_session("J1 first send")
    await journeys.seed_replies([{"content": "COLD_OK", "finish_reason": "stop"}])

    frames = await one_turn(journeys, thread_id, "what is on my plate today?")

    assert "COLD_OK" in "".join(assistant_texts(frames)), frame_types(frames)
    rows = await journeys.messages(thread_id)
    sent = by_content(rows, "what is on my plate today?")
    assert sent["delivery_state"] == "delivered", states(rows)
    assert sent["seq"] is not None, "a delivered input holds a sequence (I4)"
    assert str(sent["created_at"]).strip(), "a delivered input carries a timestamp"
    assert sent["delivery_error"] is None
    transcript = transcript_order(rows)
    assert transcript[0].startswith("[PASSIVE CONTEXT DIGEST]\nCurrent time: ")
    assert transcript[1:] == ["what is on my plate today?", "COLD_OK"]
    await assert_idle_clean(journeys, thread_id)


async def test_j1_6_reopening_the_thread_shows_the_same_transcript_and_no_stop(
    journeys,
) -> None:
    """J1.6 — leave the thread and come back to it (a reload, a second visit).

    PAST (P2, P3, P7): the transcript that comes back must be the same one,
    in the same order, with the same timestamps — and the fresh subscription
    must not hand the client a turn to keep a Stop button for.
    """
    thread_id = await journeys.create_session("J1 reload")
    await journeys.seed_replies([{"content": "RELOAD_OK", "finish_reason": "stop"}])
    await one_turn(journeys, thread_id, "before the reload")
    before = await journeys.messages(thread_id)

    async with TurnDriver(journeys.backend) as reopened:
        snapshot = await reopened.collect(
            thread_id, until=("session_snapshot",), timeout=1,
        )
        assert snapshot == [], "a socket that never subscribed received session state"
        await reopened.subscribe(thread_id)
        after = await journeys.messages(thread_id)

    assert [(r["id"], r["seq"], r["created_at"]) for r in after] == [
        (r["id"], r["seq"], r["created_at"]) for r in before
    ], f"the transcript changed across a reload: {states(before)} -> {states(after)}"
    assert await journeys.active_turn_count(thread_id) == 0, (
        "reopening the thread restored a turn that had already ended, which is "
        "the phantom Stop button of USER-REPORTS #6b"
    )


# ---------------------------------------------------------------------------
# J2 — the answer uses tools
# ---------------------------------------------------------------------------


async def test_j2_a_tool_turn_binds_its_result_and_leaves_nothing_running(
    journeys,
) -> None:
    """J2 — ask something that forces a tool call and let the turn finish.

    PAST (P1, A1): a tool block is the classic stuck spinner. When the turn is
    over, the tool must have reported a real result, the assistant must have
    continued past it, and the backend must hold no live turn for the block to
    keep spinning against.
    """
    (journeys.backend.workspace / "journey-probe.txt").write_text("PROBE PAYLOAD")
    thread_id = await journeys.create_session("J2 tools")
    await journeys.seed_replies([
        {"tool_calls": [{"id": "tc_0", "name": "read_file",
                         "arguments_json": json.dumps({"path": "journey-probe.txt"})}],
         "finish_reason": "tool_calls"},
        {"content": "TOOL_OK", "finish_reason": "stop"},
    ])

    frames = await one_turn(journeys, thread_id, "read the probe file")

    types = frame_types(frames)
    assert "tool_start" in types, types
    done = [frame for frame in frames if frame["type"] == "tool_done"]
    assert done, f"a tool started and never reported done: {types}"
    assert "PROBE PAYLOAD" in str(done[0].get("result") or ""), (
        "the tool ran but its result never came back to the block that started it"
    )
    assert "TOOL_OK" in "".join(assistant_texts(frames)), types
    await assert_idle_clean(journeys, thread_id)


async def test_j2_past_stopping_mid_tool_ends_the_turn_and_the_thread_answers(
    journeys,
) -> None:
    """J2 PAST — press Stop between the tool result and the reply it feeds.

    P11 (the race edge) plus USER-REPORTS #6a: the turn ended mid-tool and the
    UI kept a spinning block and a Stop button forever. The durable half of
    that promise is asserted here — the turn terminalises, no live turn is left
    behind, and the thread takes the next message with no repair.
    """
    (journeys.backend.workspace / "journey-probe2.txt").write_text("SECOND PROBE")
    thread_id = await journeys.create_session("J2 stop mid-tool")
    await journeys.seed_replies([
        {"tool_calls": [{"id": "tc_0", "name": "read_file",
                         "arguments_json": json.dumps({"path": "journey-probe2.txt"})}],
         "finish_reason": "tool_calls"},
        {"content": "NEVER_ARRIVES", "completion_gate": GATE, "finish_reason": "stop"},
    ])

    async with TurnDriver(journeys.backend) as driver:
        await driver.subscribe(thread_id)
        await driver.send(thread_id, "read the second probe")
        await driver.collect(thread_id, until=("tool_done",), timeout=45)
        await driver.stop(thread_id)
        stopped = await driver.collect(
            thread_id, until=("turn_cancelled", "turn_complete"), timeout=45,
        )

    assert "turn_cancelled" in frame_types(stopped), (
        f"Stop during a tool turn never terminalised it: {frame_types(stopped)}"
    )
    await assert_idle_clean(journeys, thread_id)
    await assert_thread_still_answers(journeys, thread_id, "ALIVE_AFTER_TOOL_STOP")


# ---------------------------------------------------------------------------
# J3 — queue a message behind the running turn (hold-to-queue)
# ---------------------------------------------------------------------------


async def test_j3_a_queued_input_holds_no_place_until_it_earns_one(
    journeys,
) -> None:
    """J3 — hold Send during a running turn, then watch the queue drain.

    PAST (P1, P3, A6): the chip's whole existence is `delivery_state=queued`,
    so the one-step-past check is that the state ENDS. While queued the row
    holds NULL seq and is transcript-ineligible; the instant it delivers it
    takes a sequence AFTER the reply it waited for — not the place it would
    have had if it had never waited.
    """
    thread_id = await journeys.create_session("J3 queue")
    await journeys.seed_replies([
        {"content": "FIRST_ANSWER", "completion_gate": GATE, "finish_reason": "stop"},
        {"content": "SECOND_ANSWER", "finish_reason": "stop"},
    ])

    async with TurnDriver(journeys.backend) as driver:
        await driver.subscribe(thread_id)
        await driver.send(thread_id, "the first thing")
        await driver.collect(thread_id, until=("turn_activity",), timeout=30)
        await driver.send(thread_id, "the queued thing",
                          delivery_intent="queue_after_turn")
        queued = await driver.collect(thread_id, until=("input_queued",), timeout=30)

        # OBS — the queue is acknowledged (P4: the user can tell it worked).
        receipt = [f for f in queued if f["type"] == "input_queued"][0]
        assert receipt["delivery_state"] == "queued"
        # A6 — while it waits it has no place in the transcript at all.
        waiting = by_content(await user_rows(journeys, thread_id), "the queued thing")
        assert waiting["delivery_state"] == "queued"
        assert waiting["seq"] is None, (
            "a queued input already holds a sequence, so it can render among "
            "messages older than itself before it was ever sent (A5/A6)"
        )

        await journeys.release_gate(GATE)
        await driver.collect(thread_id, until=("turn_complete",), timeout=45)
        await driver.collect(thread_id, until=("turn_complete",), timeout=45)

    rows = await settled(journeys, thread_id)
    delivered = by_content(rows, "the queued thing")
    # PAST (P1) — the cause of the chip is gone, which is what makes it leave.
    assert delivered["delivery_state"] == "delivered", states(rows)
    assert delivered["seq"] is not None
    # PAST (P3) — and it landed after the answer it deliberately waited for.
    transcript = transcript_order(await journeys.messages(thread_id))
    assert transcript[0].startswith("[PASSIVE CONTEXT DIGEST]\nCurrent time: ")
    assert transcript[1:] == [
        "the first thing", "FIRST_ANSWER", "the queued thing", "SECOND_ANSWER",
    ]
    await assert_idle_clean(journeys, thread_id)


async def test_j3_past_the_drained_queue_does_not_come_back_on_the_next_turn(
    journeys,
) -> None:
    """J3 PAST (P2) — once the queue is empty it stays empty.

    "Gone" that comes back on the next store update is the same defect wearing
    a delay. One more ordinary turn on the same thread must leave the drained
    input exactly where it landed, still delivered, still in the same place.
    """
    thread_id = await journeys.create_session("J3 non-recurrence")
    await journeys.seed_replies([
        {"content": "HOLD_ONE", "completion_gate": GATE, "finish_reason": "stop"},
        {"content": "HOLD_TWO", "finish_reason": "stop"},
    ])
    async with TurnDriver(journeys.backend) as driver:
        await driver.subscribe(thread_id)
        await driver.send(thread_id, "opening move")
        await driver.collect(thread_id, until=("turn_activity",), timeout=30)
        await driver.send(thread_id, "parked behind it",
                          delivery_intent="queue_after_turn")
        await driver.collect(thread_id, until=("input_queued",), timeout=30)
        await journeys.release_gate(GATE)
        await driver.collect(thread_id, until=("turn_complete",), timeout=45)
        await driver.collect(thread_id, until=("turn_complete",), timeout=45)
    drained = by_content(await settled(journeys, thread_id), "parked behind it")

    await assert_thread_still_answers(journeys, thread_id, "AFTER_THE_QUEUE")

    rows = await journeys.messages(thread_id)
    again = by_content(rows, "parked behind it")
    assert (again["delivery_state"], again["seq"]) == ("delivered", drained["seq"]), (
        f"the drained input moved or re-entered the queue on the next turn: "
        f"{states(rows)}"
    )
    assert not [r for r in rows if r["delivery_state"] == "queued"]


# ---------------------------------------------------------------------------
# J6 — steer: the opposite instruction to a queue
# ---------------------------------------------------------------------------


async def test_j6_a_steer_sent_mid_turn_lands_in_order_behind_the_running_step(
    journeys,
) -> None:
    """J6 — type during a running turn and press Send (not hold): steer.

    A steer and a queue are opposite instructions and must not resolve alike.
    PAST (P3, P9): the steer is delivered — not parked, not dropped — and both
    inputs read in the order they were typed, with neither duplicated.
    """
    thread_id = await journeys.create_session("J6 steer")
    await journeys.seed_replies([
        {"content": "ANSWER_ONE", "completion_gate": GATE, "finish_reason": "stop"},
        {"content": "ANSWER_TWO", "finish_reason": "stop"},
    ])

    async with TurnDriver(journeys.backend) as driver:
        await driver.subscribe(thread_id)
        await driver.send(thread_id, "start working on this")
        await driver.collect(thread_id, until=("turn_activity",), timeout=30)
        await driver.send(thread_id, "actually, also this")  # apply_now = steer
        await driver.collect(thread_id, until=("input_accepted",), timeout=30)
        await journeys.release_gate(GATE)
        await driver.collect(thread_id, until=("turn_complete",), timeout=45)
        await driver.collect(thread_id, until=("turn_complete",), timeout=45)

    rows = await settled(journeys, thread_id)
    steer = by_content(rows, "actually, also this")
    assert steer["delivery_state"] == "delivered", states(rows)
    transcript = transcript_order(await journeys.messages(thread_id))
    assert transcript[0].startswith("[PASSIVE CONTEXT DIGEST]\nCurrent time: ")
    assert transcript[1:] == [
        "start working on this", "ANSWER_ONE", "actually, also this", "ANSWER_TWO",
    ]
    await assert_idle_clean(journeys, thread_id)
