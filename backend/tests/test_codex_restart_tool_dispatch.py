"""First tool calls after a real backend restart retain exact execution owners."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from tests.contract.matrix_backend import IsolatedBackend
from tests.contract.matrix_runner import BackendClient, TurnDriver, assistant_texts, frame_types


def _write_scoped_receipt_budget_sitecustomize(
    tmp_path, *, stale_call_ids, delay_call_id=None, delay_seconds=0.0,
):
    """A sitecustomize.py giving only ``stale_call_ids`` the short budget.

    Optionally also delays when a different call's receipt becomes visible
    (``delay_call_id``), to prove that call still keeps the full budget.
    """
    startup = tmp_path / "startup"
    startup.mkdir(exist_ok=True)
    lines = [
        "import os",
        "import sys",
        "sys.path.insert(0, os.getcwd())",
        "from tests.contract.receipt_budget_scope import install_scoped_receipt_budget",
        f"install_scoped_receipt_budget({sorted(stale_call_ids)!r})",
    ]
    if delay_call_id:
        lines += [
            "import time",
            "from subagents.codex_durable_receipts import RolloutReceiptCursor",
            "_original_read_receipt = RolloutReceiptCursor.read_receipt",
            "_delay_start = {}",
            "def _delayed_read_receipt(self, turn_id, call_id):",
            f"    if call_id == {delay_call_id!r}:",
            "        start = _delay_start.setdefault(call_id, time.monotonic())",
            f"        if time.monotonic() - start < {delay_seconds!r}:",
            "            return None",
            "    return _original_read_receipt(self, turn_id, call_id)",
            "RolloutReceiptCursor.read_receipt = _delayed_read_receipt",
        ]
    (startup / "sitecustomize.py").write_text("\n".join(lines) + "\n")
    return startup


async def _send(client, thread_id, content):
    async with TurnDriver(client.backend) as driver:
        await driver.subscribe(thread_id)
        await driver.send(thread_id, content)
        frames = await driver.collect(thread_id, until=("turn_complete", "error"), timeout=15)
    assert "error" not in frame_types(frames), frames
    assert "turn_complete" in frame_types(frames), frames
    return frames


async def _read_probe(client, thread_id, call_id):
    await client.seed_replies([
        {"tool_calls": [{"id": call_id, "name": "read_file",
                         "arguments_json": json.dumps({"path": "restart-probe.txt"})}],
         "finish_reason": "tool_calls"},
        {"content": "PROBE_COMPLETE", "finish_reason": "stop"},
    ])
    frames = await _send(client, thread_id, "Read restart-probe.txt")
    done = [frame for frame in frames if frame.get("type") == "tool_done"]
    assert len(done) == 1, frames
    assert "restart dispatcher payload" in str(done[0].get("result")), frames
    assert "PROBE_COMPLETE" in assistant_texts(frames)
    assert done[0]["turn_id"] == next(
        frame["turn_id"] for frame in frames if frame.get("type") == "turn_complete"
    )
    return done[0]["turn_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_first_tools_in_existing_and_fresh_conversations_after_restart(tmp_path):
    """Exercise HTTP/WS, restart recovery, real registration, and the real tool registry."""
    backend = IsolatedBackend(tmp_path)
    await asyncio.to_thread(backend.boot)
    client = BackendClient(backend, "codex")
    (backend.workspace / "restart-probe.txt").write_text("restart dispatcher payload")
    try:
        existing = await client.create_session("existing restart conversation")
        before = await _read_probe(client, existing, "before-restart")
        await asyncio.to_thread(backend.stop)
        await asyncio.to_thread(backend.boot)
        fresh = await client.create_session("fresh post-restart conversation")
        after = []
        for label, thread_id in (("existing", existing), ("fresh", fresh)):
            await client.seed_replies([{"content": "CHAT_WORKS", "finish_reason": "stop"}])
            assert "CHAT_WORKS" in assistant_texts(await _send(client, thread_id, "Say hello"))
            after.append(await _read_probe(client, thread_id, f"{label}-after-restart"))
            assert await client.active_turn_count(thread_id) == 0
        assert len(set([before, *after])) == 3
        # Inspect only this test process's disposable database, after all sends.
        with sqlite3.connect(backend.state / "corsia.db") as db:
            for thread_id, turn_id in zip((existing, fresh), after, strict=True):
                assert db.execute(
                    "SELECT status FROM active_turns WHERE id=? AND thread_id=?",
                    (turn_id, thread_id),
                ).fetchone() == ("completed",)
    finally:
        await asyncio.to_thread(backend.stop)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unmatched_tool_receipts_fail_visibly_after_restart(tmp_path, monkeypatch):
    """The real transport exit persists a failure and releases both conversations."""
    startup = _write_scoped_receipt_budget_sitecustomize(tmp_path, stale_call_ids={"current-call"})
    backend = IsolatedBackend(tmp_path)
    environment = backend._env
    monkeypatch.setattr(backend, "_env", lambda: environment() | {"PYTHONPATH": str(startup)})
    await asyncio.to_thread(backend.boot)
    client = BackendClient(backend, "codex")
    (backend.workspace / "restart-probe.txt").write_text("restart dispatcher payload")
    try:
        existing = await client.create_session("existing failure conversation")
        await _read_probe(client, existing, "seed")
        await asyncio.to_thread(backend.stop)
        await asyncio.to_thread(backend.boot)
        fresh = await client.create_session("fresh failure conversation")
        for thread_id in (existing, fresh):
            await client.seed_replies([{
                "tool_calls": [{
                    "id": "current-call", "receipt_call_id": "stale-call",
                    "name": "write_file", "arguments": {
                        "path": "must-not-exist.txt", "content": "unexpected effect",
                    },
                }], "finish_reason": "tool_calls",
            }])
            async with TurnDriver(backend) as driver:
                await driver.subscribe(thread_id)
                await driver.send(thread_id, "Write the test file")
                frames = await driver.collect(
                    thread_id, until=("turn_complete", "error"), timeout=15,
                )
            assert "turn_complete" in frame_types(frames) or "error" in frame_types(frames), frames
            rows = await client.messages(thread_id)
            assert any(
                "Timed out waiting for the current Codex tool usage receipt after 0.1s"
                in str(row.get("content", "")) for row in rows
            ), rows
            # The failed Turn never answered the person, so one wake answers it once.
            for _ in range(150):
                if await client.active_turn_count(thread_id) == 0:
                    break
                await asyncio.sleep(0.1)
            assert await client.active_turn_count(thread_id) == 0
            rows = await client.messages(thread_id)
            assert sum(
                (row.get("metadata") or {}).get("event_type") == "system.turn.unanswered_input"
                for row in rows
            ) == 1, rows
            assert not list(backend.workspace.rglob("must-not-exist.txt"))
            await _read_probe(client, thread_id, "after-failure")
    finally:
        await asyncio.to_thread(backend.stop)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_post_failure_probe_receipt_is_not_held_to_the_stale_budget(tmp_path, monkeypatch):
    """A genuinely delayed-but-real probe receipt must not inherit the
    stale call's short budget in the same backend process (#2720)."""
    startup = _write_scoped_receipt_budget_sitecustomize(
        tmp_path, stale_call_ids={"current-call"},
        delay_call_id="after-failure", delay_seconds=0.5,
    )
    backend = IsolatedBackend(tmp_path)
    environment = backend._env
    monkeypatch.setattr(backend, "_env", lambda: environment() | {"PYTHONPATH": str(startup)})
    await asyncio.to_thread(backend.boot)
    client = BackendClient(backend, "codex")
    (backend.workspace / "restart-probe.txt").write_text("restart dispatcher payload")
    try:
        existing = await client.create_session("existing failure conversation")
        await _read_probe(client, existing, "seed")
        await client.seed_replies([{
            "tool_calls": [{
                "id": "current-call", "receipt_call_id": "stale-call",
                "name": "write_file", "arguments": {
                    "path": "must-not-exist.txt", "content": "unexpected effect",
                },
            }], "finish_reason": "tool_calls",
        }])
        async with TurnDriver(backend) as driver:
            await driver.subscribe(existing)
            await driver.send(existing, "Write the test file")
            frames = await driver.collect(
                existing, until=("turn_complete", "error"), timeout=15,
            )
        assert "turn_complete" in frame_types(frames) or "error" in frame_types(frames), frames
        rows = await client.messages(existing)
        assert any(
            "Timed out waiting for the current Codex tool usage receipt after 0.1s"
            in str(row.get("content", "")) for row in rows
        ), rows
        assert await client.active_turn_count(existing) == 0
        # The probe's receipt only becomes visible after 0.5s -- above the
        # stale budget, comfortably below the product default -- and must
        # still succeed, in the SAME process, without a restart.
        await _read_probe(client, existing, "after-failure")
    finally:
        await asyncio.to_thread(backend.stop)
