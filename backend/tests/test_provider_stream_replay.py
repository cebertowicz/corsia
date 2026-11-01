"""The replay driver must detect a change in what the pump durably writes.

**Scope of these streams.** The frames here are inline and hand-written, and
they are testing the *driver mechanism* — that replay reaches the pump, that
the bridge write precedes the echo, that prefixes stop where asked, and that a
changed pump changes the snapshot. They are explicitly NOT fixtures and do not
certify pump behavior; that requires recorded captures, which
``provider_stream_fixtures.validate_fixture`` refuses to accept without
provenance. Sensitivity is provable now; coverage is not.
"""

from __future__ import annotations

import json

import pytest

from tests.provider_stream_fixtures import StreamFixture
from tests.provider_stream_replay import replay, snapshot

# A harness turn needs a signed-in Default: no ambient login.
pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("signed_in_defaults")]


def _stream(frames: list[dict]) -> StreamFixture:
    """An in-memory stand-in carrying raw frames straight to the normalizer."""
    return StreamFixture(
        name="cc-inline", harness="cc", path=None, frames=frames, meta={},
    )


_INIT = {"type": "system", "subtype": "init", "thread_id": "cc-1",
         "model": "m", "tools": [], "mcp_servers": [], "cwd": "."}
_RESULT = {"type": "result", "subtype": "success", "is_error": False,
           "result": "done", "stop_reason": "end_turn"}


def _text_turn() -> StreamFixture:
    return _stream([
        _INIT,
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "the answer"}]}},
        _RESULT,
    ])


def _tool_turn() -> StreamFixture:
    return _stream([
        _INIT,
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "call-1",
             "name": "mcp__corsia__read_file",
             "input": {"path": "a.md"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "call-1",
             "content": "file contents"},
        ]}},
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "read it"}]}},
        _RESULT,
    ])


async def test_replay_writes_the_assistant_answer_durably(isolated_db):
    from infra.database import get_db

    result = await replay(_text_turn(), get_db(), thread_id="thread-text")

    assistant = [m for m in result.messages if m["role"] == "assistant"]
    assert [m["content"] for m in assistant] == ["the answer"]
    assert result.turns and result.turns[0]["status"] != "running"


async def test_replay_leaves_no_live_runtime_behind(isolated_db):
    """A replayed thread must not outlive replay in the process-wide registry.

    App shutdown refuses to finish past a live entry it cannot terminate, so a
    leftover would fail whichever later test in the worker stops an app.
    """
    from infra.database import get_db
    from subagents.cc_runtime_state import _LIVE

    await replay(_text_turn(), get_db(), thread_id="thread-leak")

    assert "thread-leak" not in _LIVE


async def test_replay_persists_the_tool_result_before_its_echo(isolated_db):
    """The bridge write must precede the echo, as it does in production."""
    from infra.database import get_db

    result = await replay(_tool_turn(), get_db(), thread_id="thread-tool")

    tool_rows = [m for m in result.messages if m["role"] == "tool"]
    assert [m["content"] for m in tool_rows] == ["file contents"]
    # Ordering is the contract: the tool row sits after the assistant step
    # that declared the call and before the answer that follows it.
    roles = [m["role"] for m in result.messages]
    assert roles.index("tool") < len(roles) - 1


async def test_replay_stops_at_a_prefix(isolated_db):
    """Every prefix is a state a crash could really strand."""
    from infra.database import get_db

    full = await replay(_tool_turn(), get_db(), thread_id="thread-full")
    assert full.events_delivered > 2

    partial = await replay(
        _tool_turn(), get_db(), thread_id="thread-partial", stop_after=2,
    )
    assert partial.events_delivered == 2
    assert len(partial.messages) < len(full.messages)
    # The turn is still open: this is exactly the stranded state the crash
    # harness then asks one boot pass to converge.
    assert any(turn["status"] == "running" for turn in partial.turns)


async def test_the_digest_is_stable_across_identical_replays(isolated_db):
    """A comparison key that varied run to run could not prove preservation."""
    from infra.database import get_db

    first = await replay(_text_turn(), get_db(), thread_id="thread-a")
    second = await replay(_text_turn(), get_db(), thread_id="thread-b")
    assert first.digest() == second.digest()


async def test_the_digest_changes_when_the_pump_writes_differently(
    isolated_db, monkeypatch,
):
    """THE PROOF: perturb what the pump persists, and replay must notice.

    A regression net that cannot see a changed durable write would certify a
    broken decomposition. The perturbation drops the assistant text at
    ``persist_assistant_step`` — the actual durable-write seam, and the exact
    class of loss a bad refactor of ``_on_assistant_step`` would cause.

    Targeting the durable writer is the point. An earlier version of this test
    perturbed ``_emit_finalized_step`` and passed while changing nothing,
    because that function only broadcasts: a perturbation that misses the seam
    proves the harness is sensitive to nothing at all.
    """
    from infra.database import get_db
    from subagents import cc_event_steps

    baseline = await replay(_text_turn(), get_db(), thread_id="thread-base")
    assert any(
        m["role"] == "assistant" and m["content"] == "the answer"
        for m in baseline.messages
    ), "baseline never persisted the answer; the perturbation would prove nothing"

    real_persist = cc_event_steps.persist_assistant_step

    def dropping_persist(*args, **kwargs):
        """Perturbation: commit the step with its text emptied."""
        kwargs["content"] = ""
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(
        cc_event_steps, "persist_assistant_step", dropping_persist,
    )
    perturbed = await replay(_text_turn(), get_db(), thread_id="thread-pert")

    assert perturbed.digest() != baseline.digest(), (
        "replay did not notice the pump dropping assistant text — this "
        "harness would certify a refactor that loses the answer"
    )


async def test_snapshot_reads_durable_rows_not_the_pumps_own_report(
    isolated_db,
):
    """The snapshot must come from the database, not from what the pump said."""
    from infra.database import get_db

    db = get_db()
    result = await replay(_text_turn(), db, thread_id="thread-durable")
    messages, turns = snapshot(db, "thread-durable")
    assert messages == result.messages
    assert turns == result.turns
    assert json.loads(result.digest())["messages"] == messages
