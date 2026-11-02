"""Each Native fork tool call leaves one content-free line naming its outcome."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from loguru import logger

from core import fork_native_tools
from core.checkpoint_fork_kind import CHECKPOINT_KIND, CHECKPOINT_TOOL_NAME
from llm.router import LLMResponse, ToolCallRequest
from test_checkpoint_provider_adapters import _async_value, _request

SECRET = "entry body that must never reach the log"


def _calls(*specs):
    return LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id=f"c{index}", name=name, arguments=arguments)
        for index, (name, arguments) in enumerate(specs)
    ])


async def _batch(monkeypatch, response, results, fits):
    pending = iter(results)

    async def executor(*_args):
        return next(pending)

    verdicts = iter(fits)
    monkeypatch.setattr(
        fork_native_tools, "input_fits_off_loop", lambda *_args: _async_value(next(verdicts)),
    )
    monkeypatch.setattr(
        fork_native_tools, "input_room", lambda *_args: SimpleNamespace(counted=110, bound=100),
    )
    lines = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="INFO")
    try:
        fits_room = await fork_native_tools.run_tool_calls(
            SimpleNamespace(), response, [], asyncio.Event(), _request(None), [],
            kind=replace(CHECKPOINT_KIND, tool_executor=executor),
        )
    finally:
        logger.remove(sink)
    return fits_room, [line for line in lines if line.startswith("fork.tool_call ")]


@pytest.mark.asyncio
async def test_each_call_logs_its_operation_and_outcome_without_contents(monkeypatch):
    response = _calls(
        ("read_file", {"path": "/secret"}),
        (CHECKPOINT_TOOL_NAME, {"operation": "inspect", "entry_id": 3}),
        (CHECKPOINT_TOOL_NAME, {"operation": "add", "field": "next_actions"}),
        (CHECKPOINT_TOOL_NAME, {"operation": SECRET}),
        (CHECKPOINT_TOOL_NAME, {}),
        ("read the file\nfork.tool_call forged", {}),
    )
    results = [SECRET, "Error: next_actions is not a continuity field", "staged", "staged"]

    fits_room, lines = await _batch(monkeypatch, response, results, [True] * 7)

    assert fits_room
    prefix = "fork.tool_call fork=checkpoint-1 kind=checkpoint tool="
    assert lines == [
        prefix + "read_file operation=- outcome=refused error=- result_chars="
        + str(len(CHECKPOINT_KIND.tool_refusal)),
        prefix + f"continuity_edit operation=inspect outcome=ok error=- result_chars={len(SECRET)}",
        prefix + "continuity_edit operation=add outcome=error error=other result_chars=45",
        prefix + "continuity_edit operation=other outcome=ok error=- result_chars=6",
        prefix + "continuity_edit operation=- outcome=ok error=- result_chars=6",
        prefix + "other operation=- outcome=refused error=- result_chars="
        + str(len(CHECKPOINT_KIND.tool_refusal)),
    ]
    assert not any(SECRET in line or "/secret" in line or "forged" in line for line in lines)


@pytest.mark.asyncio
async def test_a_result_over_the_room_logs_as_over_room(monkeypatch):
    response = _calls((CHECKPOINT_TOOL_NAME, {"operation": "inspect"}))

    fits_room, lines = await _batch(monkeypatch, response, ["x" * 50], [True, False, True])

    assert fits_room
    assert lines == [
        "fork.tool_call fork=checkpoint-1 kind=checkpoint tool=continuity_edit "
        "operation=inspect outcome=over_room error=- result_chars=50",
    ]


@pytest.mark.parametrize(("result", "expected"), [
    ("Error: Invalid parameters for tool 'continuity_edit': 'body' is a required property",
     "invalid_params"),
    ("Error: next_actions takes no new entries. Drop a finished one here.", "next_actions"),
    ("Error: place records an in-turn fact; use it outside a checkpoint", "in_turn_op"),
    ("Error: continuity entry 9 does not exist", "missing_entry"),
    ("Error: continuity entry 9 is retired", "inactive_entry"),
    ("Error: decisions already holds the maximum active entries", "field_full"),
    ("Error: decisions already holds the maximum 12 active entries", "field_full"),
    ("Error: checkpoint execution is no longer running", "fork_ended"),
    ("Error: replace requires entry_id", "missing_arg"),
    ("Error: " + SECRET, "other"),
    # Runtime state and model-written values never borrow a known class.
    ("Error: checkpoint ckpt-1 does not exist", "other"),
    ("Error: thread t-1 does not exist", "other"),
    ("Error: unknown continuity field: 'x does not exist'", "other"),
    ("Error: verdict written requires placement", "other"),
])
@pytest.mark.asyncio
async def test_an_error_logs_its_fixed_class_never_its_text(monkeypatch, result, expected):
    response = _calls((CHECKPOINT_TOOL_NAME, {"operation": "add"}))

    _fits, lines = await _batch(monkeypatch, response, [result], [True, True])

    assert lines == [
        "fork.tool_call fork=checkpoint-1 kind=checkpoint tool=continuity_edit "
        f"operation=add outcome=error error={expected} result_chars={len(result)}",
    ]
    assert SECRET not in lines[0]
