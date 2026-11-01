"""A cold Native fork's input ends on a user turn, whatever the source ends on."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import fork_native as fork
from core.checkpoint_fork_kind import CHECKPOINT_KIND

INSTRUCTION = "fold\n\n[CORSIA frozen continuity index]\n## facts (index)"


def _roles(messages):
    return [message["role"] for message in messages]


@pytest.mark.asyncio
async def test_source_ending_on_an_assistant_reply_gets_a_closing_user_turn(monkeypatch):
    """A manual checkpoint's range ended on a reply and the model echoed the index."""
    source = (
        {"role": "user", "content": "[CORSIA checkpoint] prior narrative"},
        {"role": "assistant", "content": "[msg a] Correction triaged; replied noted."},
    )
    # Import fork_render (via its patch) first, or it binds the stub counter for the session.
    monkeypatch.setattr(
        "core.fork_render.fit_cold_fork_input",
        lambda request, **_: SimpleNamespace(messages=source, first_included_seq=64),
    )
    monkeypatch.setattr("core.fork_cold_count.cold_input_tokens", lambda payload, writer: 7)
    tools = [{"type": "function", "function": {"name": "continuity_edit"}}]
    agent = SimpleNamespace(tools=SimpleNamespace(get_definitions=lambda: tools))
    request = SimpleNamespace(
        warm_source=None, instruction=INSTRUCTION, target="writer", max_output_tokens=8_336,
    )

    messages, _tools, first_seq = await fork._messages_and_tools(request, agent, CHECKPOINT_KIND)

    assert first_seq == 64
    assert messages[0] == {"role": "system", "content": INSTRUCTION}
    assert messages[1:3] == list(source)
    assert messages[-1] == fork.COLD_CLOSING_TURN
    assert _roles(messages) == ["system", "user", "assistant", "user"]


def test_source_ending_on_a_user_turn_keeps_alternation():
    """A range ending on a tool result (quoted as user) merges the close into it."""
    source = (
        {"role": "assistant", "content": "[msg b] calling a tool"},
        {"role": "user", "content": "[msg c] Entry 2 retired."},
    )

    messages = fork.cold_messages(INSTRUCTION, source)

    assert _roles(messages) == ["system", "assistant", "user"]
    assert messages[-1]["content"] == (
        "[msg c] Entry 2 retired.\n\n" + fork.COLD_CLOSING_TURN["content"]
    )
    assert source[-1]["content"] == "[msg c] Entry 2 retired."
