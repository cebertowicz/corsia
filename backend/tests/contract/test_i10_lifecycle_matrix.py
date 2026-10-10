"""I10 — the turn-lifecycle matrix is the merge gate for turn/delivery code.

Contract clause (PLAN, Phase 0 deliverable): the matrix
``{plain, tool, stop-mid-generation, retry, checkpoint-unavailable, queued-preemption, pressure,
restart-recovery} x {native, claude-code, codex}`` runs against the ISOLATED
environment and gates anything touching turn, delivery, or checkpoint code.
Every cell the harness cannot honestly cover is reported uncovered with its
reason; silent truncation of coverage is itself a violation (the post-mortem's
"no silent caps" rule), so the uncovered set is pinned below and may not grow
without someone editing this file.

The cells are not hypothetical. Each was a production break in the week of
2026-08-03..07 while the 6900-test suite stayed green:

- ``tool``                — the first tool-using turn on any thread failed.
- ``stop-mid-generation`` — Stop during generation wedged the thread.
- ``pressure``            — a mid-turn pressure pause killed the harness.
- ``restart-recovery``    — events stuck undelivered across a restart.

E2 drives a real checkpoint refusal: nothing unattended can release it (R16),
so the exact input fails visibly and unwritten, and after a real cap or model
correction Retry delivers one new input while the failed record stays as it
was. The separate Retry cell seeds an ordinary terminal failure through the
owners, then drives the real Retry action and delivery, reporting that fixture
limit.
The added checkpoint cell has the same explicit CC/Codex transport gaps; none
of the original cells was removed or newly classified as uncovered.

Each scenario drives one path the way a user drives it (HTTP create, WebSocket
auth/subscribe/send, event stream read back) and returns ``(evidence,
violation)``. Nothing here weakens an assertion to go green: a red cell names
the broken clause and reads as the worklist for the Phase 1 reactor.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import NoReturn

import pytest

from tests.contract.matrix_input_recovery import scenario_checkpoint_unavailable, scenario_retry
from tests.contract.matrix_runner import (
    CHECKPOINT_REPLY,
    HARNESSES,
    SCENARIOS,
    BackendClient,
    BackendUnavailable,
    TurnDriver,
    assistant_texts,
    frame_types,
    render_table,
    run_matrix,
)

pytestmark = pytest.mark.contract

# A generation the crash cells are guaranteed to interrupt rather than outlive.
UNENDING_MS = 120_000
PROBE_FILE = "contract-matrix-probe.txt"
PROBE_TEXT = "matrix probe payload"

# Every harness has a deterministic, network-confined protocol fixture. Any
# uncovered cell is therefore a regression in this merge gate, not a baseline.
EXPECTED_UNCOVERED: set[tuple[str, str]] = set()


async def _one_turn(client: BackendClient, thread_id: str, text: str) -> list[dict]:
    """Send one message on a fresh socket and read to the turn's terminal frame."""
    async with TurnDriver(client.backend) as driver:
        await driver.subscribe(thread_id)
        await driver.send(thread_id, text)
        return await driver.collect(
            thread_id, until=("turn_complete", "error"), timeout=60,
        )


async def _followup_send(
    client: BackendClient, thread_id: str, evidence: list[str],
) -> tuple[list[str], str | None]:
    """Qualify reuse of the same thread after its first useful answer."""
    await client.seed_replies([{"content": "FOLLOWUP_OK", "finish_reason": "stop"}])
    frames = await _one_turn(client, thread_id, "Can this thread answer again?")
    types = frame_types(frames)
    evidence.append(f"followup_frames={types}")
    if ("error" in types or "turn_complete" not in types
            or "FOLLOWUP_OK" not in assistant_texts(frames)):
        return evidence, "same-thread follow-up send did not produce a completed answer"
    return evidence, None


async def _user_rows(client: BackendClient, thread_id: str) -> list[dict]:
    """Durable user inputs of a thread — the delivery contract's ground truth."""
    rows = await client.messages(thread_id)
    return [row for row in rows if row.get("role") == "user"]


async def _settle(client: BackendClient, thread_id: str, timeout: float) -> list[dict]:
    """Poll durable rows until no input is still ``queued``, or time runs out."""
    end = time.monotonic() + timeout
    rows = await _user_rows(client, thread_id)
    while time.monotonic() < end:
        if not [row for row in rows if row.get("delivery_state") == "queued"]:
            return rows
        await asyncio.sleep(1.0)
        rows = await _user_rows(client, thread_id)
    return rows


@pytest.mark.asyncio
async def test_recovery_observation_uses_terminal_already_received_with_snapshot(
    monkeypatch,
) -> None:
    driver = TurnDriver.__new__(TurnDriver)
    received = [
        {"type": "turn_complete", "thread_id": "s1", "turn_id": "t1"},
        {"type": "session_snapshot", "thread_id": "s1", "turn_id": "t1"},
    ]

    async def subscribe(thread_id: str) -> list[dict]:
        assert thread_id == "s1"
        return list(received)

    async def collect(*args, **kwargs) -> NoReturn:
        raise AssertionError(
            "a terminal frame already received must prevent a second collect"
        )

    monkeypatch.setattr(driver, "subscribe", subscribe)
    monkeypatch.setattr(driver, "collect", collect)

    recovered, violation = await driver.observe_recovery("s1", timeout=0.01)

    assert recovered == received
    assert violation is None


@pytest.mark.asyncio
async def test_recovery_observation_waits_for_snapshot_active_turn(
    monkeypatch,
) -> None:
    driver = TurnDriver.__new__(TurnDriver)
    received = [
        {"type": "turn_complete", "thread_id": "s1", "turn_id": "old"},
        {"type": "session_snapshot", "thread_id": "s1", "turn_id": "active"},
    ]
    active_terminal = {
        "type": "turn_complete", "thread_id": "s1", "turn_id": "active",
    }

    async def subscribe(thread_id: str) -> list[dict]:
        assert thread_id == "s1"
        return list(received)

    async def collect(*args, **kwargs) -> list[dict]:
        return [active_terminal]

    monkeypatch.setattr(driver, "subscribe", subscribe)
    monkeypatch.setattr(driver, "collect", collect)

    recovered, violation = await driver.observe_recovery("s1", timeout=0.01)

    assert recovered == received + [active_terminal]
    assert violation is None


@pytest.mark.asyncio
async def test_recovery_observation_rejects_terminal_without_snapshot(
    monkeypatch,
) -> None:
    driver = TurnDriver.__new__(TurnDriver)
    received = [{"type": "error", "thread_id": "s1", "message": "closed"}]

    async def subscribe(thread_id: str) -> list[dict]:
        assert thread_id == "s1"
        return received

    async def collect(*args, **kwargs) -> NoReturn:
        raise AssertionError(
            "a missing authoritative snapshot must fail before collection"
        )

    monkeypatch.setattr(driver, "subscribe", subscribe)
    monkeypatch.setattr(driver, "collect", collect)

    recovered, violation = await driver.observe_recovery("s1", timeout=0.01)

    assert recovered == received
    assert violation == "subscription never delivered an authoritative session snapshot"


def _states(rows: list[dict]) -> list[tuple]:
    """Compact (text, delivery_state, delivery_error) triples for evidence lines."""
    return [(str(r.get("content", ""))[:20], r.get("delivery_state"),
             r.get("delivery_error")) for r in rows]


async def scenario_plain(client: BackendClient) -> tuple[list[str], str | None]:
    """A first message on a new thread must stream and complete."""
    thread_id = await client.create_session("contract matrix plain")
    await client.seed_replies([{"content": "PLAIN_OK", "finish_reason": "stop"}])
    frames = await _one_turn(client, thread_id, "plain hello")
    evidence = [f"frames={frame_types(frames)}"]
    if "PLAIN_OK" not in assistant_texts(frames):
        return evidence, "plain turn never delivered its assistant text"
    if "turn_complete" not in frame_types(frames):
        return evidence, "plain turn never reached turn_complete"
    return await _followup_send(client, thread_id, evidence)


async def scenario_tool(client: BackendClient) -> tuple[list[str], str | None]:
    """The FIRST turn on a NEW thread calls a tool, binds the result, and finishes.

    Production incident: the first tool-using turn on any thread failed. The
    freshness is the whole point — a second-turn tool call did not reproduce it.
    """
    (client.backend.workspace / PROBE_FILE).write_text(PROBE_TEXT)
    thread_id = await client.create_session("contract matrix tool")
    await client.seed_replies([
        {"tool_calls": [{"id": "tc_0", "name": "read_file",
                         "arguments_json": json.dumps({"path": PROBE_FILE})}],
         "finish_reason": "tool_calls"},
        {"content": "TOOL_OK", "finish_reason": "stop"},
    ])
    frames = await _one_turn(client, thread_id, "read the probe file")
    types = frame_types(frames)
    done = [f for f in frames if f.get("type") == "tool_done"]
    evidence = [f"frames={types}", f"tool_done={[f.get('result') for f in done]}"]
    if "tool_start" not in types or not done:
        return evidence, "first tool-using turn on a fresh thread never ran the tool"
    if PROBE_TEXT not in str(done[0].get("result") or ""):
        return evidence, "tool ran but its result was not bound back into the turn"
    if "TOOL_OK" not in assistant_texts(frames) or "turn_complete" not in types:
        return evidence, "tool turn never continued to a completed assistant reply"
    return await _followup_send(client, thread_id, evidence)


async def scenario_stop(client: BackendClient) -> tuple[list[str], str | None]:
    """Stop mid-generation ends the turn AND leaves the thread able to run again.

    Production incident: Stop during generation wedged the thread. The recorded
    no-wedge rule (I2/I5) says a stopped thread still accepts the next input.
    """
    thread_id = await client.create_session("contract matrix stop")
    gate = f"matrix-stop-{uuid.uuid4().hex}"
    await client.seed_replies([
        {"content": "HELD", "completion_gate": gate, "finish_reason": "stop"},
    ])
    async with TurnDriver(client.backend) as driver:
        await driver.subscribe(thread_id)
        await driver.send(thread_id, "hold this generation")
        await driver.collect(thread_id, until=("turn_activity",), timeout=30)
        await driver.stop(thread_id)
        stopped = await driver.collect(
            thread_id, until=("turn_cancelled", "turn_complete"), timeout=30,
        )
        await client.release_gate(gate)
        # Re-seeded after the Stop on purpose: a cancelled turn may die before it
        # ever calls the provider, so the held reply must not be assumed spent.
        await client.seed_replies([{"content": "AFTER_STOP", "finish_reason": "stop"}])
        await driver.send(thread_id, "still alive after Stop?")
        after = await driver.collect(
            thread_id, until=("turn_complete", "error"), timeout=60,
        )
    evidence = [f"stop_frames={frame_types(stopped)}", f"after={frame_types(after)}"]
    if "turn_cancelled" not in frame_types(stopped):
        return evidence, "Stop mid-generation never produced turn_cancelled"
    if "AFTER_STOP" not in assistant_texts(after):
        return evidence, "thread wedged after Stop: the next input never delivered"
    return evidence, None


async def scenario_queued_preemption(
    client: BackendClient,
) -> tuple[list[str], str | None]:
    """An input sent mid-turn joins the ONE FIFO and delivers with no user action.

    Contract I1/I2: a thread's undelivered inputs live in exactly one FIFO, and
    every accepted input reaches ``delivered`` — here, in send order.
    """
    thread_id = await client.create_session("contract matrix queue")
    gate = f"matrix-queue-{uuid.uuid4().hex}"
    await client.seed_replies([
        {"content": "FIRST_HELD", "completion_gate": gate, "finish_reason": "stop"},
        {"content": "SECOND_FROM_QUEUE", "finish_reason": "stop"},
    ])
    async with TurnDriver(client.backend) as driver:
        await driver.subscribe(thread_id)
        await driver.send(thread_id, "first")
        await driver.collect(thread_id, until=("turn_activity",), timeout=30)
        await driver.send(thread_id, "second", delivery_intent="queue_after_turn")
        queued = await driver.collect(thread_id, until=("input_queued",), timeout=30)
        await client.release_gate(gate)
        first = await driver.collect(thread_id, until=("turn_complete",), timeout=45)
        second = await driver.collect(thread_id, until=("turn_complete",), timeout=45)
    texts = assistant_texts(queued + first + second)
    rows = await _settle(client, thread_id, timeout=30)
    evidence = [f"queued={frame_types(queued)}", f"texts={texts}",
                f"rows={_states(rows)}"]
    if "input_queued" not in frame_types(queued):
        return evidence, "mid-turn input was never acknowledged into the FIFO"
    if texts[:2] != ["FIRST_HELD", "SECOND_FROM_QUEUE"]:
        return evidence, f"queued input did not deliver in FIFO order: {texts}"
    return evidence, None


async def scenario_pressure(client: BackendClient) -> tuple[list[str], str | None]:
    """A send under context pressure folds warm and still delivers the head.

    Production incident: a mid-turn pressure pause killed the harness. Contract
    I6: a checkpoint never blocks accepting input, and the fold costs exactly
    one provider call — a second one is the retry cascade this cell forbids.
    """
    thread_id = await client.create_session("contract matrix pressure")
    await client.seed_pressured_history(thread_id)
    await client.seed_replies([
        CHECKPOINT_REPLY,
        {"content": "PRESSURED_HEAD_DELIVERED", "finish_reason": "stop"},
    ])
    frames = await _one_turn(client, thread_id, "new " * 3_000)
    calls = await client.call_count()
    evidence = [f"frames={frame_types(frames)}", f"provider_calls={calls}"]
    errors = [f for f in frames if f.get("type") == "error"]
    if errors:
        return evidence, f"pressured send surfaced a user-facing error: {errors[0]}"
    if "PRESSURED_HEAD_DELIVERED" not in assistant_texts(frames):
        return evidence, "pressured send never delivered the accepted head"
    if calls != 2:
        return evidence, f"fold+delivery cost {calls} provider calls, not the contracted 2"
    return await _followup_send(client, thread_id, evidence)


async def scenario_restart_recovery(
    client: BackendClient,
) -> tuple[list[str], str | None]:
    """A queued input survives a crash and delivers after one boot, unaided.

    Production incident: restart-recovery events stuck undelivered. Contract I2
    (two outcomes) and I8 (crash convergence) applied to the live process: the
    FIFO head delivers or is visibly paused, and never sits invisible.
    """
    thread_id = await client.create_session("contract matrix restart")
    await client.seed_replies([{"content": "HELD", "delay_ms": UNENDING_MS}])
    async with TurnDriver(client.backend) as driver:
        await driver.subscribe(thread_id)
        await driver.send(thread_id, "in flight when the process dies")
        await driver.collect(thread_id, until=("turn_activity",), timeout=30)
        await driver.send(thread_id, "queued behind it",
                          delivery_intent="queue_after_turn")
        await driver.collect(thread_id, until=("input_queued",), timeout=30)
    before = await client.messages(thread_id)
    # The crash and the boot are the whole cell: nothing below asks the system
    # for anything, so whatever recovers must recover unaided. The reply text is
    # deliberately not asserted — recovery can outrun a post-boot reseed, and a
    # test that raced it would be testing the harness, not the contract.
    client.backend.restart_after_crash()
    async with TurnDriver(client.backend) as driver:
        recovered, recovery_violation = await driver.observe_recovery(
            thread_id, timeout=60,
        )
    if recovery_violation:
        return [f"recovered={frame_types(recovered)}"], recovery_violation
    rows = await _settle(client, thread_id, timeout=60)
    after = await client.messages(thread_id)
    replies = [r for r in after if r.get("role") == "assistant"]
    events = [(str(r.get("content"))[:26], r.get("delivery_state"), r.get("next_attempt_at"))
              for r in after if r.get("role") == "system"]
    evidence = [f"post_boot_frames={frame_types(recovered)}", f"rows={_states(rows)}",
                f"system_rows_after_boot={events}"]
    unfinished = [s for s in _states(rows) if s[1] == "queued"]
    if unfinished:
        return evidence, f"input still queued one boot after the crash: {unfinished}"
    if len(replies) <= len([r for r in before if r.get("role") == "assistant"]):
        return evidence, (
            "both inputs were marked delivered after the crash but the thread "
            "never answered: a delivered input with no turn is a silent drop"
        )
    return evidence, None


SCENARIO_DRIVERS = {
    "plain": scenario_plain,
    "tool": scenario_tool,
    "stop-mid-generation": scenario_stop,
    "retry": scenario_retry,
    "checkpoint-unavailable": scenario_checkpoint_unavailable,
    "queued-preemption": scenario_queued_preemption,
    "pressure": scenario_pressure,
    "restart-recovery": scenario_restart_recovery,
}


def test_every_named_scenario_has_a_driver() -> None:
    """The matrix may not shrink silently: every named scenario has a driver."""
    missing = [name for name in SCENARIOS if name not in SCENARIO_DRIVERS]
    assert not missing, f"scenarios declared but never driven: {missing}"


@pytest.mark.integration
def test_turn_lifecycle_matrix(tmp_path) -> None:
    """Drive every cell against the isolated backend and publish the scoreboard."""
    try:
        results = asyncio.run(run_matrix(SCENARIO_DRIVERS, tmp_path / uuid.uuid4().hex))
    except BackendUnavailable as unavailable:
        pytest.skip(f"isolated backend cannot boot here: {unavailable}")
    table = render_table(results)
    print("\n" + table)
    expected = {(scenario, harness) for scenario in SCENARIOS for harness in HARNESSES}
    actual = [(result.scenario, result.harness) for result in results]
    invalid = [(result.cell, result.status) for result in results
               if result.status not in {"covered", "uncovered", "failed"}]
    duplicates = len(actual) - len(set(actual))
    assert set(actual) == expected and not duplicates and not invalid, (
        f"invalid matrix inventory: missing={sorted(expected - set(actual))}; "
        f"unexpected={sorted(set(actual) - expected)}; duplicate rows={duplicates}; "
        f"invalid statuses={invalid}\n\n{table}"
    )
    failed = [r.cell for r in results if r.status == "failed"]
    regressed = sorted(
        f"{r.scenario} x {r.harness}"
        for r in results
        if r.status == "uncovered" and (r.scenario, r.harness) not in EXPECTED_UNCOVERED
    )
    assert not failed and not regressed, (
        f"{len(failed)} cell(s) violate the turn-lifecycle contract"
        + (f"; {len(regressed)} newly uncovered cell(s) {regressed} — coverage may "
           "not shrink without editing EXPECTED_UNCOVERED" if regressed else "")
        + f"\n\n{table}\n"
    )
