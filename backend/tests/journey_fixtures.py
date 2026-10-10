"""Shared driving surface for the user-journey suite.

The journeys themselves live in ``tests/test_user_journeys.py``; this module
holds only the fixture and the read helpers they share, so the journeys read as
prose instead of plumbing.

Nothing here is a new harness. The process, the client and the socket are the
maintained isolated-backend driver from ``tests/contract/matrix_runner.py`` —
the same one the turn-lifecycle matrix and the no-wedge acceptance suite drive.
A private copy would be a second harness to keep in step, which is the failure
this project exists to stop repeating.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from tests.contract.matrix_runner import (
    BackendClient,
    BackendUnavailable,
    IsolatedBackend,
    TurnDriver,
)

#: A generation the test releases by hand rather than out-waiting. Every hold in
#: the suite is a gate, so "during the turn" is a fact and not a race.
GATE = "journey-hold"


@pytest.fixture(scope="module")
def journeys(tmp_path_factory):
    """One isolated backend for every journey in the module; skipped, never faked.

    Module-scoped because booting uvicorn per journey would cost more than the
    journeys do. The mock reply queue is process-global, so journeys run
    sequentially and each seeds its own replies before it sends anything.
    """
    root = tmp_path_factory.mktemp("journeys") / uuid.uuid4().hex
    backend = IsolatedBackend(root)
    try:
        backend.boot()
    except BackendUnavailable as unavailable:
        pytest.skip(f"isolated backend cannot boot here: {unavailable}")
    try:
        yield BackendClient(backend)
    finally:
        backend.stop()


async def one_turn(
    client: BackendClient, thread_id: str, text: str, timeout: float = 60,
) -> list[dict]:
    """Send one message on a fresh socket and read to the turn's terminal frame."""
    async with TurnDriver(client.backend) as driver:
        await driver.subscribe(thread_id)
        await driver.send(thread_id, text)
        return await driver.collect(
            thread_id, until=("turn_complete", "error"), timeout=timeout,
        )


async def user_rows(client: BackendClient, thread_id: str) -> list[dict]:
    """The thread's durable user inputs — the delivery contract's ground truth."""
    return [row for row in await client.messages(thread_id) if row["role"] == "user"]


async def settled(
    client: BackendClient, thread_id: str, timeout: float = 30,
) -> list[dict]:
    """Poll durable user rows until none is still ``queued``, or time runs out.

    Deliberately returns rather than raises: a journey asserts what it found, so
    a thread that never settles fails on the assertion that names the journey
    step, not on a timeout inside a helper.
    """
    end = time.monotonic() + timeout
    rows = await user_rows(client, thread_id)
    while time.monotonic() < end:
        if not [row for row in rows if row["delivery_state"] == "queued"]:
            return rows
        await asyncio.sleep(0.25)
        rows = await user_rows(client, thread_id)
    return rows


def by_content(rows: list[dict], content: str) -> dict:
    """The single row with this exact content, asserting it is single (A5).

    "No message ever renders twice" is not a separate test to remember; it is
    what every lookup in this suite already checks, because a duplicate makes
    the lookup itself fail.
    """
    matches = [row for row in rows if row["content"] == content]
    assert len(matches) == 1, (
        f"expected exactly one durable row saying {content!r}, found "
        f"{len(matches)}: {states(matches)}"
    )
    return matches[0]


def states(rows: list[dict]) -> list[tuple]:
    """Compact (text, role, state, error, seq) tuples for assertion messages."""
    return [
        (str(row.get("content", ""))[:28], row.get("role"),
         row.get("delivery_state"), row.get("delivery_error"), row.get("seq"))
        for row in rows
    ]


def transcript_order(rows: list[dict]) -> list[str]:
    """Contents in the order a reader would read them: by sequence, ascending.

    A row with no sequence is transcript-ineligible by definition
    (``infra/delivery_states.py``), so it is not in the reading order at all.
    """
    placed = [row for row in rows if row.get("seq") is not None]
    return [str(row["content"]) for row in sorted(placed, key=lambda r: int(r["seq"]))]


async def assert_idle_clean(client: BackendClient, thread_id: str) -> None:
    """P12 — the state a thread must be in once everything has settled.

    Asserted after every terminal event in this suite rather than at the end of
    one dedicated test: "it finished" and "it finished cleanly" are different
    claims, and only the second one is what the user sees.
    """
    rows = await settled(client, thread_id)
    waiting = [row for row in rows if row["delivery_state"] == "queued"]
    assert not waiting, f"thread still holds queued input at rest: {states(waiting)}"
    assert await client.active_turn_count(thread_id) == 0, (
        "the backend still reports an active turn on this thread after "
        "everything settled, so the composer would still offer Stop for a turn "
        "that has ended (A12)"
    )


async def assert_thread_still_answers(
    client: BackendClient, thread_id: str, marker: str,
) -> None:
    """A9 — the thread takes a new message with no repair call in between."""
    from tests.contract.matrix_runner import assistant_texts, frame_types

    await client.seed_replies([{"content": marker, "finish_reason": "stop"}])
    frames = await one_turn(client, thread_id, f"still alive? {marker}")
    assert marker in "".join(assistant_texts(frames)), (
        f"thread wedged: a new message never produced an answer "
        f"(frames={frame_types(frames)})"
    )
