"""Claude Code usage is recorded per model call, keyed by the call's message id.

A Turn may span several model calls. Each call's receipt is keyed by its
message id, but the measurement itself comes from ``message_delta`` once the
call closes, not from the assistant message that shares that id -- that
message's own usage is a still-generating snapshot, never the receipt.
CORSIA used to sum those snapshots in memory and write one Turn total at the
end -- lost with the process if the Turn died first, and attributable only to
the Turn as a whole. Each call is now its own ledger receipt, durable as it
arrives and idempotent by message id, so a replayed event is the same call
and a restarted process finds what was already recorded. The Turn-total
write remains only as the fallback for a harness that reported no per-call
usage.
"""

from __future__ import annotations

import pytest

from cc_runtime_fixtures import _seed_parent, child_db  # noqa: F401
from core.turn_core import TurnIdentity
from infra.provider_usage import TokenUsage
from subagents import cc_event_messages, cc_event_usage
from subagents.cc_runtime_state import _Live

MESSAGE = {
    "id": "msg_01", "model": "claude-opus-4-1", "role": "assistant",
    "content": [{"type": "text", "text": "done"}],
    "usage": {"input_tokens": 40, "output_tokens": 12, "cache_read_input_tokens": 8},
}


def _live() -> _Live:
    live = _Live(session=None, parent_thread_id=None, name="cc", model="opus")
    live.provider_reported_model = "opus"
    live.corsia_turn = TurnIdentity("cc-turn", 1)
    live.turn_active = True
    return live


def test_an_assistant_message_never_reports_its_own_usage() -> None:
    """An assistant frame's usage is a still-generating snapshot, not a receipt.

    The provider's authoritative total for a call only exists once
    ``message_delta`` closes it (see the ``final_call_usage`` tests below);
    an assistant frame -- one per content block, several may share a call's
    message id -- must never itself produce a usage event, or a stale
    snapshot could win the ledger receipt.
    """
    events = cc_event_messages.from_assistant(MESSAGE)

    assert [e["kind"] for e in events] == ["assistant_step"]


@pytest.mark.asyncio
async def test_each_call_is_one_receipt_keyed_by_its_message_id(monkeypatch) -> None:
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"args": args, **kwargs})

    monkeypatch.setattr(cc_event_usage, "run_in_db", capture)
    live = _live()

    await cc_event_usage.record_call_llm_usage(
        "cc-thread", live, message_id="msg_01", usage=MESSAGE["usage"],
    )
    await cc_event_usage.record_call_llm_usage(
        "cc-thread", live, message_id="msg_02", usage={"input_tokens": 5, "output_tokens": 1},
    )

    assert [c["usage_key"] for c in captured] == ["message:msg_01", "message:msg_02"]
    assert all(c["turn_id"] == "cc-turn" for c in captured)
    assert all(c["source"] == "external_harness:claude-code" for c in captured)
    first = captured[0]["args"][2]
    assert (first["prompt_tokens"], first["cache_read_tokens"], first["completion_tokens"]) == (
        40, 8, 12,
    )
    assert "cost_usd" not in first


@pytest.mark.asyncio
async def test_the_terminal_hands_the_ledger_the_turns_whole_volume(monkeypatch) -> None:
    """Whether the Turn's calls were receipted is the ledger's to say, not the runtime's."""
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"fn": fn, "args": args, **kwargs})

    monkeypatch.setattr(cc_event_usage, "run_in_db", capture)
    live = _live()
    # Two calls streamed: 40/8/12 with an id, 5/0/1 without one.
    live.turn_usage = TokenUsage(input_uncached=45, cache_read=8, output=13)
    await cc_event_usage.record_call_llm_usage(
        "cc-thread", live, message_id="msg_01", usage=MESSAGE["usage"],
    )

    await cc_event_usage.record_terminal_llm_usage(
        "cc-thread", live, {"input_tokens": 5, "output_tokens": 1},
    )

    assert [c["usage_key"] for c in captured] == ["message:msg_01", "terminal"]
    assert captured[1]["fn"] is cc_event_usage.record_turn_fallback
    volume = captured[1]["args"][2]
    assert (volume["prompt_tokens"], volume["cache_read_tokens"], volume["completion_tokens"]) == (45, 8, 13)
    assert live.turn_usage is None


@pytest.mark.asyncio
async def test_a_turn_that_reported_no_usage_hands_the_ledger_an_unknown_volume(monkeypatch) -> None:
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"args": args, **kwargs})

    monkeypatch.setattr(cc_event_usage, "run_in_db", capture)
    live = _live()

    await cc_event_usage.record_terminal_llm_usage("cc-thread", live, None)

    assert [c["usage_key"] for c in captured] == ["terminal"]
    assert captured[0]["args"][2] == {}


@pytest.mark.asyncio
async def test_a_receipt_the_entry_cannot_even_record_escapes_here(monkeypatch) -> None:
    """The entry owns every receipt failure as a defect; only its own failure escapes."""
    from infra.llm_usage import PlanUsagePersistenceError

    async def cannot_record(fn, *args, **kwargs):
        raise PlanUsagePersistenceError("plan-bound usage defect could not be recorded")

    monkeypatch.setattr(cc_event_usage, "run_in_db", cannot_record)
    with pytest.raises(PlanUsagePersistenceError):
        await cc_event_usage.record_call_llm_usage(
            "cc-thread", _live(), message_id="msg_01", usage=MESSAGE["usage"],
        )


def test_a_replayed_call_is_the_same_receipt_and_a_restart_keeps_the_first(isolated_db) -> None:
    from infra.database import get_db
    from infra.llm_usage import record_llm_usage
    from tests.external_harness_usage_helpers import persist_turn

    persist_turn("cc-thread", "cc-turn")
    measured = {"prompt_tokens": 48, "completion_tokens": 12, "total_tokens": 60}
    for _replay in range(2):
        record_llm_usage(
            "cc-thread", "opus", measured, source="external_harness:claude-code",
            turn_id="cc-turn", usage_key="message:msg_01",
        )
    record_llm_usage(
        "cc-thread", "opus", {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        source="external_harness:claude-code", turn_id="cc-turn", usage_key="message:msg_02",
    )

    db = get_db()
    try:
        rows = db.execute(
            "SELECT total_tokens FROM llm_usage WHERE turn_id='cc-turn' ORDER BY total_tokens"
        ).fetchall()
        assert [r["total_tokens"] for r in rows] == [6, 60]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_call_is_recorded_under_the_model_the_provider_named_for_it(monkeypatch) -> None:
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"args": args, **kwargs})

    monkeypatch.setattr(cc_event_usage, "run_in_db", capture)
    live = _live()  # the session reports "opus"
    await cc_event_usage.record_call_llm_usage(
        "cc-thread", live, message_id="msg_01", usage=MESSAGE["usage"], model="claude-sonnet-4",
    )
    await cc_event_usage.record_call_llm_usage(
        "cc-thread", live, message_id="msg_02", usage=MESSAGE["usage"],
    )

    assert [c["args"][1] for c in captured] == ["claude-code/claude-sonnet-4", "claude-code/opus"]


@pytest.mark.asyncio
async def test_a_call_the_ledger_cannot_take_is_the_turns_defect(monkeypatch) -> None:
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"fn": fn, **kwargs})

    monkeypatch.setattr(cc_event_usage, "run_in_db", capture)
    live = _live()
    live.provider_reported_model = None
    await cc_event_usage.record_call_llm_usage(
        "cc-thread", live, message_id="msg_01", usage=MESSAGE["usage"],
    )
    await cc_event_usage.record_terminal_llm_usage("cc-thread", live, {"input_tokens": 4, "output_tokens": 2})

    assert [(c["fn"], c["reason"], c["usage_key"]) for c in captured] == [
        (cc_event_usage.report_unrecordable_call, "model_unknown", "message:msg_01"),
        (cc_event_usage.report_unrecordable_call, "model_unknown", "terminal"),
    ]


@pytest.mark.asyncio
async def test_a_call_without_a_reported_model_uses_the_selected_one(monkeypatch) -> None:
    """A session with no reported model yet still receipts under the catalog selection."""
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"fn": fn, "args": args, **kwargs})

    monkeypatch.setattr(cc_event_usage, "run_in_db", capture)
    live = _live()
    live.provider_reported_model = None
    live.expected_reported_model = "sonnet"

    await cc_event_usage.record_call_llm_usage(
        "cc-thread", live, message_id="msg_01", usage=MESSAGE["usage"],
    )

    assert [c["fn"] for c in captured] == [cc_event_usage.record_observed_call_usage]
    assert captured[0]["args"][1] == "claude-code/sonnet"
    assert captured[0]["usage_key"] == "message:msg_01"


@pytest.mark.asyncio
async def test_a_terminal_without_a_reported_model_uses_the_selected_one(monkeypatch) -> None:
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"fn": fn, "args": args, **kwargs})

    monkeypatch.setattr(cc_event_usage, "run_in_db", capture)
    live = _live()
    live.provider_reported_model = None
    live.expected_reported_model = "sonnet"

    await cc_event_usage.record_terminal_llm_usage(
        "cc-thread", live, {"input_tokens": 4, "output_tokens": 2},
    )

    assert [c["fn"] for c in captured] == [cc_event_usage.record_turn_fallback]
    assert captured[0]["args"][1] == "claude-code/sonnet"
    assert captured[0]["usage_key"] == "terminal"


@pytest.mark.asyncio
async def test_the_true_final_usage_replaces_every_stale_assistant_snapshot(
    monkeypatch, child_db,
) -> None:
    """Reproduces the captured wire order for one call (#1750, gh comment
    5719356490): ``message_start`` (output=4) then two ``assistant`` frames
    sharing one message id -- one per content block, both still reading the
    in-progress output=4 -- then ``message_delta`` (output=462, the true
    final). Exactly one receipt lands, at the true final count, even though
    two frames shared the call's message id.
    """
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_stream import CCStreamSession

    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"args": args, **kwargs})

    _seed_parent(child_db, sid="cc-thread")
    monkeypatch.setattr(cc_event_usage, "run_in_db", capture)
    live = _live()
    on_event = _make_on_event(live, "cc-thread", lambda *_a: None, db=None)
    session = CCStreamSession(binary="claude", session_id="cc-thread", on_event=on_event)

    message_id = "msg_011Cf9VKeUfDhCaDYuLwHuuD"
    stale_usage = {
        "input_tokens": 2, "cache_creation_input_tokens": 6715,
        "cache_read_input_tokens": 2662, "output_tokens": 4,
    }

    await session._dispatch_raw_event({
        "type": "stream_event", "event": {
            "type": "message_start", "message": {
                "id": message_id, "model": "claude-opus-4-1", "role": "assistant",
                "content": [], "stop_reason": None, "usage": stale_usage,
            },
        },
    })
    await session._dispatch_raw_event({
        "type": "assistant", "message": {
            "id": message_id, "model": "claude-opus-4-1", "role": "assistant",
            "content": [{"type": "thinking", "thinking": "reasoning", "signature": "sig"}],
            "stop_reason": None, "usage": stale_usage,
        },
    })
    await session._dispatch_raw_event({
        "type": "assistant", "message": {
            "id": message_id, "model": "claude-opus-4-1", "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": None, "usage": stale_usage,
        },
    })
    await session._dispatch_raw_event({
        "type": "stream_event", "event": {
            "type": "message_delta", "usage": {"output_tokens": 462},
        },
    })

    assert len(captured) == 1
    receipt = captured[0]
    assert receipt["usage_key"] == f"message:{message_id}"
    row = receipt["args"][2]
    assert (row["prompt_tokens"], row["cache_read_tokens"], row["completion_tokens"]) == (
        2, 2662, 462,
    )
    assert captured[0]["args"][1] == "claude-code/claude-opus-4-1"


@pytest.mark.asyncio
async def test_final_call_usage_without_a_prior_message_start_is_not_receipted(
    monkeypatch, child_db,
) -> None:
    """A ``message_delta`` whose ``message_start`` was dropped or reordered
    never gets the identity merge, so it carries only a partial reading
    (here, a bare ``output_tokens``) and no message id. Receipting that
    partial payload would write a wrong row missing the cache fields; it
    must instead be recorded as its own distinct defect.
    """
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_stream import CCStreamSession

    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"fn": fn, **kwargs})

    _seed_parent(child_db, sid="cc-thread")
    monkeypatch.setattr(cc_event_usage, "run_in_db", capture)
    live = _live()
    on_event = _make_on_event(live, "cc-thread", lambda *_a: None, db=None)
    session = CCStreamSession(binary="claude", session_id="cc-thread", on_event=on_event)

    await session._dispatch_raw_event({
        "type": "stream_event", "event": {
            "type": "message_delta", "usage": {"output_tokens": 5},
        },
    })

    assert [c["fn"] for c in captured] == [cc_event_usage.report_unrecordable_call]
    assert captured[0]["reason"] == "measurement_incomplete"
    assert captured[0]["usage_key"] == "call:1"


@pytest.mark.asyncio
async def test_final_call_usage_with_no_usage_at_all_is_not_receipted(
    monkeypatch, child_db,
) -> None:
    """The same dropped-identity case with no usage on the delta either."""
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_stream import CCStreamSession

    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"fn": fn, **kwargs})

    _seed_parent(child_db, sid="cc-thread")
    monkeypatch.setattr(cc_event_usage, "run_in_db", capture)
    live = _live()
    on_event = _make_on_event(live, "cc-thread", lambda *_a: None, db=None)
    session = CCStreamSession(binary="claude", session_id="cc-thread", on_event=on_event)

    await session._dispatch_raw_event({
        "type": "stream_event", "event": {"type": "message_delta"},
    })

    assert [c["fn"] for c in captured] == [cc_event_usage.report_unrecordable_call]
    assert captured[0]["reason"] == "measurement_incomplete"
    assert captured[0]["usage_key"] == "call:1"
