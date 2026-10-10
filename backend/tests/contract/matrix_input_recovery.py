"""I10 input recovery: a refused or failed input ends visibly and Retry revives it."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from contextlib import closing

from tests.contract.matrix_runner import (
    BACKEND_ROOT,
    CHECKPOINT_REPLY,
    TOKEN,
    BackendClient,
    TurnDriver,
    assistant_texts,
    frame_types,
)


def _input_row(client: BackendClient, thread_id: str, message_id: str) -> dict:
    """Read exact private identity/write facts only from this isolated backend."""
    uri = (client.backend.state / "corsia.db").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM messages WHERE thread_id=? AND id=?",
            (thread_id, message_id),
        ).fetchone()
        assert row is not None, "the accepted input disappeared"
        return dict(row)


def _unfittable_but_recoverable_content(source_window: int) -> str:
    """Cross the pressured source wall but fit after its old history is folded."""
    return "new " * ((source_window * 3) // 4 + 1)


def _seed_failed_input(client: BackendClient, thread_id: str) -> str:
    """Settle an ordinary no-write failure through the owners, not SQL edits.

    This fixture does not claim to provoke a real provider failure. The Retry
    action and its new input's provider dispatch below are driven over HTTP/WS.
    """
    completed = subprocess.run(
        client.backend.command([sys.executable, "-c", '''
import sys
from infra.input_writes import begin_batch_write, settle_batch_write
from infra.thread_driver import admit_input, finish_turn
from infra.time import utc_now_iso

thread_id = sys.argv[1]
admission = admit_input(
    thread_id, role="user", content="retry this failed input", intent="apply_now",
    message_id=thread_id + ":failed", turn_id=thread_id + ":turn",
    boot_epoch="matrix-retry", admitted_at=utc_now_iso(),
)
assert admission.batch is not None
begin_batch_write(admission.batch, attempt_id=thread_id + ":attempt", started_at=utc_now_iso())
settle_batch_write(thread_id + ":attempt", outcome="proved_no_write",
                   error="fixture: provider refused before writing")
terminal = finish_turn(thread_id + ":turn", outcome="error", ended_at=utc_now_iso(),
                       successor_turn_id=thread_id + ":unused", boot_epoch="matrix-retry")
assert terminal.successor is None
''', thread_id]),
        cwd=BACKEND_ROOT, env=client.backend._env(), check=False,
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return thread_id + ":failed"


async def scenario_retry(client: BackendClient) -> tuple[list[str], str | None]:
    """Real Retry admits and delivers one new input, preserving the failed audit."""
    thread_id = await client.create_session("contract matrix retry")
    message_id = _seed_failed_input(client, thread_id)
    before = _input_row(client, thread_id, message_id)
    assert before["delivery_state"] == "failed"
    assert before["write_outcome"] == "proved_no_write"
    await client.seed_replies([{"content": "RETRY_OK", "finish_reason": "stop"}])
    async with TurnDriver(client.backend) as driver:
        await driver.subscribe(thread_id)
        retry = await client.message_action(thread_id, message_id, "retry")
        frames = await driver.collect(
            thread_id, until=("turn_complete", "error"), timeout=60,
        )
    revived_id = retry.get("revived_message_id")
    assert retry.get("retried") and revived_id and revived_id != message_id
    revived = _input_row(client, thread_id, revived_id)
    assert revived["content"] == before["content"]
    assert revived["delivery_state"] == "delivered"
    assert revived["write_outcome"] == "accepted"
    assert _input_row(client, thread_id, message_id) == before, "Retry rewrote its terminal audit"
    rows = [row for row in await client.messages(thread_id) if row["role"] == "user"]
    assert {row["id"] for row in rows} == {message_id, revived_id} and len(rows) == 2
    assert "RETRY_OK" in assistant_texts(frames) and "turn_complete" in frame_types(frames)
    assert await client.call_count() == 1, "Retry dispatched more than once"
    return [
        "LIMIT: terminal failure seeded through canonical owners; no real provider failure injected",
        f"failed={message_id}; retried={revived_id}; delivered once; original audit unchanged",
    ], None


async def collect_marked_turn(
    driver: TurnDriver, thread_id: str, marker: str, timeout: float,
) -> list[dict]:
    """Collect until the Turn that says ``marker`` completes.

    A refused Turn can publish its own terminal after its visible error, so
    the first ``turn_complete`` is not necessarily the one being waited for.
    """
    end = time.monotonic() + timeout
    frames: list[dict] = []
    while time.monotonic() < end:
        frames += await driver.collect(
            thread_id, until=("turn_complete",), timeout=max(end - time.monotonic(), 0),
        )
        types = frame_types(frames)
        if "__socket_closed__" in types:
            break
        if marker in assistant_texts(frames) and types and types[-1] == "turn_complete":
            break
    return frames


async def _assert_checkpoint_failure(
    client: BackendClient, thread_id: str, message_id: str, content: str,
) -> dict:
    """Require the refused input to end visibly failed, unwritten, with its reason.

    A refusal nothing unattended can release is not a hold (R16): the exact
    input fails so the FIFO behind it keeps moving.
    """
    failed = _input_row(client, thread_id, message_id)
    assert failed["content"] == content, "the failed input's content changed"
    assert failed["delivery_state"] == "failed", "checkpoint refusal left the input waiting"
    assert failed["write_outcome"] == "proved_no_write", failed["write_outcome"]
    assert failed["seq"] is not None, "a visible failure takes its place in the transcript"
    assert failed["input_order"] is not None, "the failed input lost its FIFO order"
    assert failed["target_model"] == client.pressure_model, failed["target_model"]
    assert "minimum provider continuation exceeds the context window" in (
        failed["delivery_error"] or ""
    ), failed["delivery_error"]
    visible = next(row for row in await client.messages(thread_id) if row["id"] == message_id)
    for key in ("content", "delivery_state", "write_outcome", "delivery_error", "seq", "active_turn_id"):
        assert visible[key] == failed[key], f"public checkpoint failure differs on {key}"
    assert await client.active_turn_count(thread_id) == 0, "checkpoint refusal left an active Turn"
    return failed


async def scenario_checkpoint_unavailable(
    client: BackendClient,
) -> tuple[list[str], str | None]:
    """An actual unfittable send fails visibly; after the fix, Retry delivers it once."""
    thread_id = await client.create_session("contract matrix checkpoint unavailable")
    window = await client.seed_pressured_history(thread_id)
    prior_ids = {row["id"] for row in await client.messages(thread_id) if row["role"] == "user"}
    content = _unfittable_but_recoverable_content(window)
    profile = f"/api/models/{client.pressure_model}/profile?token={TOKEN}"
    recovery_profile = profile.replace(client.pressure_model, client.model, 1)
    try:
        await client.seed_replies([CHECKPOINT_REPLY])
        async with TurnDriver(client.backend) as driver:
            await driver.subscribe(thread_id)
            message_id = await driver.send(thread_id, content)
            failed = await driver.collect(thread_id, until=("error",), timeout=90)
            assert "error" in frame_types(failed), (
                "checkpoint unavailability was not surfaced to the live client"
            )
            refused = await _assert_checkpoint_failure(client, thread_id, message_id, content)
            await client.seed_replies([{"content": "RETRY_RECOVERED", "finish_reason": "stop"}])
            if client.harness == "native":
                await client._request("PATCH", profile, json={"context_window": 393_216})
            else:
                # The recovery model checkpoints at the CORSIA default unless
                # its own window is saved; save it so the retry fits unfolded.
                await client._request(
                    "PATCH", recovery_profile, json={"context_window": 393_216},
                )
                await client._request(
                    "PATCH",
                    f"/api/threads/{thread_id}?token={TOKEN}",
                    json={"model": client.model},
                )
            assert _input_row(client, thread_id, message_id) == refused, (
                "fixing the cap or model silently revived a failed input"
            )
            retry = await client.message_action(thread_id, message_id, "retry")
            recovered = await collect_marked_turn(driver, thread_id, "RETRY_RECOVERED", 60)
        revived_id = retry.get("revived_message_id")
        assert retry.get("retried") and revived_id and revived_id != message_id, (
            f"Retry did not admit a new input: {retry}"
        )
        assert "RETRY_RECOVERED" in assistant_texts(recovered), (
            f"the Retry Turn never answered: {frame_types(recovered)}"
        )
        assert "turn_complete" in frame_types(recovered)
        revived = _input_row(client, thread_id, revived_id)
        assert revived["content"] == refused["content"], "Retry changed the input content"
        if client.harness == "native":
            assert revived["target_model"] == refused["target_model"], revived["target_model"]
            assert revived["target_execution_epoch"] == refused["target_execution_epoch"], (
                revived["target_execution_epoch"], refused["target_execution_epoch"],
            )
        else:
            assert revived["target_model"] == client.model, revived["target_model"]
            assert revived["target_execution_epoch"] > refused["target_execution_epoch"], (
                revived["target_execution_epoch"], refused["target_execution_epoch"],
            )
        assert revived["delivery_state"] == "delivered" and revived["write_outcome"] == "accepted", (
            f"Retry input settled {revived['delivery_state']}/{revived['write_outcome']}"
        )
        assert revived["seq"] is not None, "the delivered Retry input has no transcript seq"
        assert _input_row(client, thread_id, message_id) == refused, "Retry rewrote the failed record"
        rows = [row for row in await client.messages(thread_id) if row["role"] == "user"]
        assert {row["id"] for row in rows} == prior_ids | {message_id, revived_id}, (
            "user rows are not the prior rows plus the failed and Retry inputs"
        )
        assert len(rows) == len(prior_ids) + 2, "Retry duplicated the input"
        assert await client.call_count() == 1, "Retry dispatched more than once"
        return [
            f"input={message_id}; failed visibly unwritten; retried={revived_id}; "
            "delivered once; failed record unchanged"
        ], None
    finally:
        # Remove this cell before restoring the shared mock cap, including on
        # assertion failure; restoration must not wake a leftover input.
        await client._request("DELETE", f"/api/threads/{thread_id}?token={TOKEN}")
        if client.harness == "native":
            await client._request("PATCH", profile, json={"context_window": window})
        else:
            await client._request("PATCH", recovery_profile, json={"context_window": None})
