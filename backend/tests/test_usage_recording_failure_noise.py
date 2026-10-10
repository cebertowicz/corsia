"""Usage-recording failures exist only for turns that reached a provider."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from external_harness_usage_helpers import bind_plan_turn, persist_turn
from infra.database import get_db
from plans import usage_calls


SOURCE = "external_harness:claude-code"


def _failure_events(node_id: str) -> list[dict]:
    db = get_db()
    try:
        return [
            json.loads(row["payload_json"])
            for row in db.execute(
                "SELECT payload_json FROM plan_node_events "
                "WHERE node_id=? AND event='usage_recording_failed' ORDER BY rowid",
                (node_id,),
            ).fetchall()
        ]
    finally:
        db.close()


def _terminal_status(turn_id: str, status: str) -> None:
    db = get_db()
    try:
        db.execute("UPDATE active_turns SET status=? WHERE id=?", (status, turn_id))
        db.commit()
    finally:
        db.close()


def test_failed_before_provider_does_not_record_a_missing_measurement(isolated_db):
    persist_turn("failed-thread", "failed-turn")
    _plan, node_id, *_rest = bind_plan_turn("failed-thread", "failed-turn")
    _terminal_status("failed-turn", "error")

    usage_calls.record_turn_fallback(
        "failed-thread", "opus", {}, source=SOURCE, turn_id="failed-turn",
        quiet_if_unmeasured=True,
    )

    assert _failure_events(node_id) == []


def test_errored_turn_with_output_and_no_measurement_remains_a_recording_failure(isolated_db):
    persist_turn("output-thread", "output-turn")
    _plan, node_id, *_rest = bind_plan_turn("output-thread", "output-turn")
    _terminal_status("output-turn", "error")

    usage_calls.record_turn_fallback(
        "output-thread", "opus", {}, source=SOURCE, turn_id="output-turn",
        quiet_if_unmeasured=False,
    )

    assert _failure_events(node_id) == [{
        "turn_id": "output-turn",
        "usage_key": "terminal",
        "reason": "measurement_missing",
    }]


@pytest.mark.asyncio
async def test_failed_codex_tool_output_without_measurement_remains_a_recording_failure(
    isolated_db,
):
    from subagents.codex_runtime_terminal import settle_failed_turn_usage

    persist_turn("codex-tool-thread", "codex-tool-turn")
    _plan, node_id, *_rest = bind_plan_turn("codex-tool-thread", "codex-tool-turn")
    _terminal_status("codex-tool-turn", "error")
    live = SimpleNamespace(
        provider_reported_model="gpt-5-codex",
        harness="codex",
        corsia_turn=SimpleNamespace(turn_id="codex-tool-turn"),
        # _on_tool_use persisted this step; it is model output even though
        # turn_has_assistant_row stays false for a tool-only turn.
        last_assistant_message_id="tool-step",
        turn_has_assistant_row=False,
    )

    await settle_failed_turn_usage("codex-tool-thread", live, {})

    assert _failure_events(node_id) == [{
        "turn_id": "codex-tool-turn",
        "usage_key": "terminal",
        "reason": "measurement_missing",
    }]


def test_errored_turn_with_measurement_records_its_usage(isolated_db):
    persist_turn("errored-thread", "errored-turn")
    _plan, node_id, *_rest = bind_plan_turn("errored-thread", "errored-turn")
    _terminal_status("errored-turn", "error")

    usage_calls.record_turn_fallback(
        "errored-thread", "opus", {"total_tokens": 7}, source=SOURCE,
        turn_id="errored-turn",
    )

    db = get_db()
    try:
        assert [row["total_tokens"] for row in db.execute(
            "SELECT total_tokens FROM llm_usage WHERE turn_id=?", ("errored-turn",),
        ).fetchall()] == [7]
    finally:
        db.close()
    assert _failure_events(node_id) == []


def test_completed_turn_without_measurement_remains_a_recording_failure(isolated_db):
    persist_turn("measured-thread", "measured-turn")
    _plan, node_id, *_rest = bind_plan_turn("measured-thread", "measured-turn")
    _terminal_status("measured-turn", "completed")

    usage_calls.record_turn_fallback(
        "measured-thread", "opus", {}, source=SOURCE, turn_id="measured-turn",
    )

    assert _failure_events(node_id) == [{
        "turn_id": "measured-turn",
        "usage_key": "terminal",
        "reason": "measurement_missing",
    }]
