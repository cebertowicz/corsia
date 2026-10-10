"""One projection per tool row: the live frame and the reload read the same."""

from __future__ import annotations

import asyncio
import json

import pytest

from core.message_display import _convert_assistant_for_display
from core.turn_core import TurnIdentity
from tools.tool_display import SettledResult, ToolDisplay, project_tool_display

ROW_FIELDS = ("phrase", "outcome", "error_line")


@pytest.mark.parametrize(("settled", "outcome", "error_line"), [
    (SettledResult("contents"), "done", None),
    (SettledResult("Error executing bash: boom\ntrace"), "failed", "Error executing bash: boom"),
    (SettledResult("\n  denied  \nmore", is_error=True), "failed", "denied"),
    (SettledResult("nothing ran", is_error=True, synthetic_reason="undispatched_tool"), "refused", None),
    (SettledResult("cancelled", synthetic_reason="user_interruption"), "unknown", None),
    (SettledResult("Error: gone", is_error=True, synthetic_reason="user_interruption"), "unknown", None),
    (SettledResult("no result", is_error=True, synthetic_reason="provider_missing_result"), "unknown", None),
    (SettledResult("no result", is_error=True, synthetic_reason="turn_ended_without_result"), "unknown", None),
])
def test_projector_reads_the_settled_result(settled, outcome, error_line):
    display = project_tool_display("bash", {"command": "ls"}, settled)
    assert display == ToolDisplay("Running ls", outcome, error_line, "ls")


def test_running_call_claims_no_outcome():
    display = project_tool_display("read_file", '{"path": "/a/b.md"}', None)
    assert display.fields() == {"phrase": "Reading b.md", "object": "/a/b.md"}
    assert display.outcome_fields() == {}


@pytest.mark.parametrize("bridged", [False, True])
def test_review_request_names_document_live_and_on_reload(bridged):
    from tools.tool_display import settled_display_call

    arguments = {"doc_path": "shared/x/NOTE.md", "reviewer": "2dddc476", "client_id": "c1"}
    name = "doc_review_request"
    if bridged:
        name = "mcp__corsia__tool_call"
        arguments = {"name": "doc_review_request", "arguments": arguments}
    live = project_tool_display(name, arguments, None)
    assert live.phrase == "Requesting review NOTE.md"
    stored = settled_display_call({"name": name, "args": arguments}, "Review queued.", {})
    assert stored["phrase"] == live.phrase
    assert stored["object"] == "shared/x/NOTE.md"
    assert stored["outcome"] == "done"


def test_native_error_string_reads_failed_without_a_stored_flag():
    """A native row stored before any error flag existed reads as it failed."""
    legacy = SettledResult.from_row("Error: Invalid parameters for tool 'bash'", {})
    assert project_tool_display("bash", {}, legacy).outcome == "failed"


def _reload(step_calls: list[dict], results: dict[str, dict]) -> dict[str, dict]:
    row = {"role": "assistant", "content": "", "metadata": json.dumps({"llm_tool_calls": step_calls})}
    calls = json.loads(_convert_assistant_for_display(row, results)["metadata"])["tool_calls"]
    return {call["id"]: {key: call.get(key) for key in ROW_FIELDS} for call in calls}


def _row_fields(frame: dict) -> dict:
    return {key: frame.get(key) for key in ROW_FIELDS}


@pytest.mark.parametrize("streamed", [False, True])
def test_native_live_rows_match_reload(isolated_db, streamed):
    from core.step_handler import StepHandler
    from core.turns_steps import USER_INTERRUPTION_TOOL_RESULT
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
        "VALUES ('t-native', 'u', 'omlx@local/test', 't', 't')"
    )
    db.commit()
    db.close()
    frames: list[dict] = []

    async def bcast(payload: dict) -> None:
        frames.append(payload)

    handler = StepHandler(
        thread_id="t-native", model_info={"model": "test/model"},
        broadcast=bcast, turn_id="turn-1",
    )
    handler.bind_turn_identity(TurnIdentity("turn-1", 1))
    calls = {
        "ok": {"path": "/w/a.md"},
        "bad": {"path": "/w/b.md"},
        "cut": {"path": "/w/c.md"},
    }
    if streamed:
        for call_id in calls:
            asyncio.run(handler.handle("tool_start", {
                "tool_id": call_id, "name": "read_file", "args": '{"pa',
            }))
    asyncio.run(handler.handle("tool_calls_start", {
        "tools": [
            {"id": call_id, "name": "read_file", "hint": "", "args": json.dumps(args, indent=2)}
            for call_id, args in calls.items()
        ],
        "content": "",
        "tool_call_dicts": [
            {"id": call_id, "type": "function",
             "function": {"name": "read_file", "arguments": json.dumps(args)}}
            for call_id, args in calls.items()
        ],
        "skip_broadcast": streamed,
    }))
    asyncio.run(handler.handle("tool_complete", {"id": "ok", "name": "read_file", "result": "text"}))
    asyncio.run(handler.handle("tool_complete", {
        "id": "bad", "name": "read_file", "result": "Error executing read_file: missing\ndetail",
    }))
    asyncio.run(handler.handle("tool_complete", {
        "id": "cut", "name": "read_file", "result": USER_INTERRUPTION_TOOL_RESULT,
        "synthetic_tool_result": True, "synthetic_reason": "user_interruption",
    }))
    asyncio.run(handler.handle("tool_calls_done", {"tool_ids": list(calls)}))

    db = get_db()
    rows = [dict(row) for row in db.execute(
        "SELECT role, content, metadata FROM messages WHERE thread_id='t-native' ORDER BY rowid"
    ).fetchall()]
    db.close()
    step = json.loads(rows[0]["metadata"])
    results = {json.loads(row["metadata"])["tool_call_id"]: row for row in rows[1:]}
    # The native writer stamps the canonical error flag on a real error result only.
    assert {
        call_id: json.loads(row["metadata"]).get("is_error") for call_id, row in results.items()
    } == {"ok": None, "bad": True, "cut": None}
    reload = _reload(step["llm_tool_calls"], results)

    done = {frame["tool_id"]: _row_fields(frame) for frame in frames if frame["type"] == "tool_done"}
    final = next(frame for frame in frames if frame["type"] == "step_text")
    finalized = {call["id"]: _row_fields(call) for call in final["metadata"]["tool_calls"]}
    assert done == finalized == reload
    assert reload == {
        "ok": {"phrase": "Reading a.md", "outcome": "done", "error_line": None},
        "bad": {"phrase": "Reading b.md", "outcome": "failed",
                "error_line": "Error executing read_file: missing"},
        "cut": {"phrase": "Reading c.md", "outcome": "unknown", "error_line": None},
    }


def _bridge_row(call_id: str, content: str, *, synthetic_reason: str | None = None) -> dict:
    """A tool row as the Claude Code and Codex bridges persist it."""
    from core.tool_result_delivery import _result_metadata, synthetic_result_identity
    from infra.tool_result_errors import is_error_result

    extra = None
    is_error = is_error_result(content)
    if synthetic_reason:
        _, extra = synthetic_result_identity(tool_use_id=call_id, reason=synthetic_reason)
        is_error = True
    metadata = _result_metadata(
        turn_id="turn-1", assistant_step_id="step-1", provider_call_id=call_id,
        invocation_id=f"inv-{call_id}", name="mcp__corsia__bash",
        arguments={"command": "ls"}, content=content, is_error=is_error,
        extra_metadata=extra,
    )
    return {"id": f"row-{call_id}", "role": "tool", "content": content, "metadata": json.dumps(metadata)}


def test_harness_live_rows_match_reload():
    """Claude Code and Codex frames read the bound canonical result of the same row."""
    from core._tool_result_delivery_support import result_from_row

    rows = {
        "ok": _bridge_row("ok", "listing"),
        "bad": _bridge_row("bad", "Error executing bash: denied"),
        "undispatched": _bridge_row("undispatched", "Bash is not served", synthetic_reason="undispatched_tool"),
        "lost": _bridge_row("lost", "No result", synthetic_reason="provider_missing_result"),
    }
    args = {"command": "ls"}
    live = {}
    for call_id, row in rows.items():
        start = project_tool_display("mcp__corsia__bash", args, None)
        done = project_tool_display(
            "mcp__corsia__bash", None, SettledResult.from_result(result_from_row(row)),
        )
        live[call_id] = {"phrase": start.phrase, "outcome": done.outcome, "error_line": done.error_line}
    step_calls = [
        {"id": call_id, "type": "function",
         "function": {"name": "mcp__corsia__bash", "arguments": json.dumps(args)}}
        for call_id in rows
    ]
    assert live == _reload(step_calls, rows)
    assert [live[call_id]["outcome"] for call_id in rows] == ["done", "failed", "refused", "unknown"]
