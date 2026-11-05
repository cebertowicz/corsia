"""A cold Native fork's input closes on the instruction itself, as a warm one does."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import fork_native as fork
from core.checkpoint_fork_kind import CHECKPOINT_KIND

INSTRUCTION = "[CORSIA frozen continuity index]\n## facts (index)\n\nWrite the checkpoint narrative."


def _roles(messages):
    return [message["role"] for message in messages]


@pytest.mark.asyncio
async def test_source_ending_on_an_assistant_reply_closes_on_the_instruction(monkeypatch):
    """A manual checkpoint's range ended on a reply; a closing pointer to a
    system instruction got a turn-ending agent note instead of a narrative."""
    source = (
        {"role": "user", "content": "[CORSIA checkpoint] prior narrative"},
        {"role": "assistant", "content": "[msg a] Correction triaged; replied noted."},
    )
    monkeypatch.setattr("core.fork_cold_count.cold_input_tokens", lambda payload, writer: 7)
    monkeypatch.setattr(
        "core.fork_render.fit_cold_fork_input",
        lambda request, **_: SimpleNamespace(messages=source, first_included_seq=64),
    )
    tools = [{"type": "function", "function": {"name": "continuity_edit"}}]
    agent = SimpleNamespace(tools=SimpleNamespace(get_definitions=lambda: tools))
    request = SimpleNamespace(
        warm_source=None, instruction=INSTRUCTION, target="writer", max_output_tokens=8_336,
    )

    messages, _tools, first_seq = await fork._messages_and_tools(request, agent, CHECKPOINT_KIND)

    assert first_seq == 64
    assert messages == [*source, {"role": "user", "content": INSTRUCTION}]
    assert all(message["content"] != INSTRUCTION for message in messages[:-1])


def test_source_ending_on_a_user_turn_ends_on_the_instruction():
    """A range ending on a tool result (quoted as user) merges the instruction after it."""
    source = (
        {"role": "assistant", "content": "[msg b] calling a tool"},
        {"role": "user", "content": "[msg c] Entry 2 retired."},
    )

    messages = fork.cold_messages(INSTRUCTION, source)

    assert _roles(messages) == ["assistant", "user"]
    assert messages[-1]["content"] == "[msg c] Entry 2 retired.\n\n" + INSTRUCTION
    assert messages[-1]["content"].endswith("Write the checkpoint narrative.")
    assert source[-1]["content"] == "[msg c] Entry 2 retired."
