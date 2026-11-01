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
    )
    results = [SECRET, "Error: next_actions is not a continuity field", "staged", "staged"]

    fits_room, lines = await _batch(monkeypatch, response, results, [True] * 6)

    assert fits_room
    prefix = "fork.tool_call fork=checkpoint-1 kind=checkpoint tool="
    assert lines == [
        prefix + "read_file operation=- outcome=refused result_chars="
        + str(len(CHECKPOINT_KIND.tool_refusal)),
        prefix + f"continuity_edit operation=inspect outcome=ok result_chars={len(SECRET)}",
        prefix + "continuity_edit operation=add outcome=error result_chars=45",
        prefix + "continuity_edit operation=other outcome=ok result_chars=6",
        prefix + "continuity_edit operation=- outcome=ok result_chars=6",
    ]
    assert not any(SECRET in line or "/secret" in line for line in lines)


@pytest.mark.asyncio
async def test_a_result_over_the_room_logs_as_over_room(monkeypatch):
    response = _calls((CHECKPOINT_TOOL_NAME, {"operation": "inspect"}))

    fits_room, lines = await _batch(monkeypatch, response, ["x" * 50], [True, False, True])

    assert fits_room
    assert lines == [
        "fork.tool_call fork=checkpoint-1 kind=checkpoint tool=continuity_edit "
        "operation=inspect outcome=over_room result_chars=50",
    ]
