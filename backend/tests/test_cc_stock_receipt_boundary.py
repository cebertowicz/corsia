"""Actual stock-Claude MCP behavior with synthetic inference and exact-port egress."""

import asyncio
import json

import pytest

from cc_receipt_stock_runtime import StockClaudeRuntime

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login


def _assert_stored_outcome(runtime):
    from infra.database import get_db

    scenario = runtime.scenario
    db = get_db()
    try:
        turn = db.execute("SELECT status FROM active_turns WHERE id='turn-current'").fetchone()
        assert turn["status"] == ("error" if scenario == "required_failure" else "completed")
        results = db.execute("SELECT content FROM messages WHERE thread_id='held-thread' AND role='tool' AND content LIKE 'canonical fixture result%'").fetchall()
        expected = 0 if scenario == "mcp_error_control" else (2 if scenario == "slow_concurrent" else 1)
        assert len(results) == expected
        assert len(runtime.executed) == expected
        if scenario != "required_failure":
            assert db.execute("SELECT 1 FROM messages WHERE thread_id='held-thread' AND content LIKE '%fixture answer 2%'").fetchone()
    finally:
        db.close()
    terminals = [frame for frame in runtime.frames if frame["type"] in {"turn_complete", "turn_cancelled"}]
    assert len(terminals) == 1 and terminals[0]["type"] == "turn_complete"


async def _later_send(runtime):
    from cc_runtime_fixtures import feed_main_with_persisted_input
    from infra.database import get_db
    from subagents.cc_runtime_state import _LIVE

    successor = _LIVE.get("held-thread")
    await successor.session._join_terminal_event()
    if successor.session._terminal_driver is not None:
        await asyncio.shield(successor.session._terminal_driver)
    runtime.complete.clear()
    await feed_main_with_persisted_input("held-thread", "Later normal user send", target=runtime.target)
    await asyncio.wait_for(runtime.complete.wait(), 10)
    assert len(runtime.fixture.requests) == 3
    assert _LIVE.get("held-thread") is successor
    db = get_db()
    try:
        assert db.execute("SELECT 1 FROM messages WHERE thread_id='held-thread' AND role='user' AND content LIKE '%Later normal user send%'").fetchone()
        assert db.execute("SELECT 1 FROM messages WHERE thread_id='held-thread' AND role='assistant' AND content LIKE '%fixture answer 3%'").fetchone()
    finally:
        db.close()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["required_failure", "cutover", "slow_concurrent", "mcp_error_control"])
async def test_stock_claude_receipt_fence_and_continuation(isolated_db, tmp_path, monkeypatch, scenario):
    from subagents.cc_runtime_state import _LIVE

    runtime = StockClaudeRuntime(scenario, tmp_path, monkeypatch)
    fixture = runtime.fixture
    try:
        await runtime.open()
        if scenario == "slow_concurrent":
            await asyncio.sleep(30.1)
            assert runtime.session.returncode is None and not fixture.results
        fixture.release.set()
        if scenario == "cutover":
            await asyncio.wait_for(runtime.waiting.wait(), 5)
            assert len(fixture.requests) == 1
            runtime.release_checkpoint.set()
        await asyncio.wait_for(runtime.complete.wait(), 20)
        if scenario == "required_failure":
            assert runtime.session._terminal_driver is not None
            await asyncio.wait_for(asyncio.shield(runtime.session._terminal_driver), 5)
            assert len(fixture.requests) == 1
            # The completed effect stays retained; the failure names the
            # checkpoint refusal's reason, not a generic settlement text (#1921).
            assert fixture.errors == ["fixture required checkpoint unavailable"]
            assert runtime.session.returncode is not None
        else:
            assert fixture.next_request.is_set() and len(fixture.requests) == 2, json.dumps(runtime.raw_results)
            if scenario == "mcp_error_control":
                assert fixture.errors == ["MCP errors are consumable"]
                errors = [block for message in fixture.requests[1]["messages"]
                          for block in message["content"] if isinstance(block, dict)
                          and block.get("type") == "tool_result" and block.get("is_error")]
                assert len(errors) == 1 and "500" in json.dumps(errors)
            else:
                assert not fixture.errors
            if scenario == "cutover":
                assert len(runtime.sessions) == 2 and runtime.session.returncode is not None
                successor = _LIVE.get("held-thread")
                assert successor is not runtime.live and successor.corsia_turn == runtime.identity
                assert (await runtime.await_checkpoint("new-checkpoint")).status == "installed"
        _assert_stored_outcome(runtime)
        if scenario == "cutover":
            await _later_send(runtime)
    finally:
        await runtime.close()
