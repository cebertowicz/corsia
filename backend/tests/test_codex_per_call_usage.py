"""Codex usage is recorded per model call, from each reading's newly spent volume.

Codex reports one thread-cumulative counter (``total``) and the last call's
usage (``last``) after each model call. CORSIA kept only the turn's baseline
and latest and wrote one delta at the turn's end -- lost with the process if
the turn died first, and attributable only to the turn as a whole. Each
reading's newly spent volume is now its own ledger receipt as it arrives,
keyed by its provider turn and ordinal, durable before the turn ends and
replay-safe for a repeated reading, which spends nothing. The turn-total delta
is handed to the ledger on the completed and the failed terminal alike, and
the ledger writes only what the readings' receipts do not hold.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from infra.provider_usage import TokenUsage
from infra.turn_volume import CumulativeTurnVolume
from subagents import codex_runtime_events, codex_runtime_terminal, codex_usage_calls


def _live() -> SimpleNamespace:
    return SimpleNamespace(
        provider_reported_model="gpt-5-codex",
        harness="codex",
        corsia_turn=SimpleNamespace(turn_id="codex-turn"),
        turn_has_assistant_row=False,
        last_assistant_message_id=None,
    )


def _row(**fields: int) -> dict:
    return {**TokenUsage(**fields).ledger_row(), "cost_usd": None}


def _reading(total: int, last: int) -> dict:
    return {
        "total": {"inputTokens": total, "outputTokens": 0},
        "last": {"inputTokens": last, "outputTokens": 0},
    }


def test_each_reading_yields_the_volume_it_newly_spent() -> None:
    tracker = CumulativeTurnVolume()

    first = tracker.observe(
        "turn-1", total=TokenUsage(input_uncached=100), last=TokenUsage(input_uncached=100),
    )
    second = tracker.observe(
        "turn-1", total=TokenUsage(input_uncached=250), last=TokenUsage(input_uncached=150),
    )
    repeated = tracker.observe(
        "turn-1", total=TokenUsage(input_uncached=250), last=TokenUsage(input_uncached=150),
    )
    third = tracker.observe(
        "turn-1", total=TokenUsage(input_uncached=400), last=TokenUsage(input_uncached=150),
    )

    assert (first, second, repeated, third) == (
        TokenUsage(input_uncached=100), TokenUsage(input_uncached=150), None,
        TokenUsage(input_uncached=150),
    )
    # The turn's whole delta is still the sum of what its readings spent.
    assert tracker.take("turn-1") == TokenUsage(input_uncached=400)


def test_a_first_reading_without_a_last_spends_nothing_it_can_name() -> None:
    tracker = CumulativeTurnVolume()

    assert tracker.observe("turn-1", total=TokenUsage(input_uncached=100), last=None) is None
    assert tracker.observe(
        "turn-1", total=TokenUsage(input_uncached=160), last=None,
    ) == TokenUsage(input_uncached=60)


@pytest.mark.asyncio
async def test_a_token_usage_notification_emits_one_call_usage_per_spent_reading() -> None:
    from subagents import codex_app_server
    from subagents.codex_app_server_notifications import handle_thread_notification

    emitted: list[dict] = []

    async def emit(event: dict) -> None:
        emitted.append(event)

    session = codex_app_server.CodexAppServerSession(binary="unused", task=None)
    session._emit = emit
    session._track_active_turn("turn-1")
    session._observe_turn_volume = MethodType(
        codex_app_server.CodexAppServerSession._observe_turn_volume, session,
    )
    for total, last in ((100, 100), (250, 150), (250, 150)):
        await handle_thread_notification(
            session, "thread/tokenUsage/updated",
            {"turnId": "turn-1", "tokenUsage": _reading(total, last)},
        )

    assert [(e["kind"], e["turn_id"], e["ordinal"]) for e in emitted] == [
        ("call_usage", "turn-1", 1), ("call_usage", "turn-1", 2),
    ]
    assert emitted[1]["usage"]["prompt_tokens"] == 150
    import json

    json.dumps(emitted)  # every Codex event is plain JSON


@pytest.mark.asyncio
async def test_each_reading_is_one_receipt_keyed_by_its_provider_turn_and_ordinal(
    monkeypatch,
) -> None:
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"args": args, **kwargs})

    monkeypatch.setattr(codex_usage_calls, "run_in_db", capture)
    monkeypatch.setattr(codex_runtime_terminal, "run_in_db", capture)
    live = _live()

    await codex_usage_calls.record_call_llm_usage(
        "codex-thread", live, provider_turn_id="turn-a", ordinal=1, measured={**TokenUsage(input_uncached=100).ledger_row(), "cost_usd": None},
    )
    await codex_usage_calls.record_call_llm_usage(
        "codex-thread", live, provider_turn_id="turn-a", ordinal=2, measured={**TokenUsage(input_uncached=150, output=7).ledger_row(), "cost_usd": None},
    )

    assert [c["usage_key"] for c in captured] == ["reading:turn-a:1", "reading:turn-a:2"]
    assert all(c["turn_id"] == "codex-turn" for c in captured)
    assert all(c["source"] == "external_harness:codex" for c in captured)
    assert all(c["harness"] == "codex" for c in captured)
    assert captured[1]["args"][2]["completion_tokens"] == 7
    assert captured[1]["args"][2]["cost_usd"] is None


@pytest.mark.asyncio
async def test_the_terminal_hands_the_ledger_the_turns_delta(monkeypatch) -> None:
    """What the readings' receipts already hold is the ledger's to say, not the runtime's."""
    calls: list[dict] = []

    async def capture(fn, *args, **kwargs):
        calls.append({"fn": fn, "args": args, **kwargs})

    monkeypatch.setattr(codex_usage_calls, "run_in_db", capture)
    monkeypatch.setattr(codex_runtime_terminal, "run_in_db", capture)
    live = _live()
    for ordinal, spent in ((1, 100), (2, 150)):
        await codex_usage_calls.record_call_llm_usage(
            "codex-thread", live, provider_turn_id="turn-a", ordinal=ordinal,
            measured=_row(input_uncached=spent),
        )

    await codex_runtime_terminal.record_terminal_llm_usage(
        "codex-thread", live, TokenUsage(input_uncached=250),
    )

    assert [c["usage_key"] for c in calls] == ["reading:turn-a:1", "reading:turn-a:2", "terminal"]
    assert all(c["harness"] == "codex" for c in calls)
    assert calls[2]["fn"] is codex_runtime_terminal.record_turn_fallback
    assert calls[2]["args"][2]["prompt_tokens"] == 250
    assert calls[2]["quiet_if_unmeasured"] is False


@pytest.mark.asyncio
async def test_a_receipt_the_entry_cannot_even_record_escapes_here(monkeypatch) -> None:
    from infra.llm_usage import PlanUsagePersistenceError

    async def cannot_record(fn, *args, **kwargs):
        raise PlanUsagePersistenceError("plan-bound usage defect could not be recorded")

    monkeypatch.setattr(codex_usage_calls, "run_in_db", cannot_record)
    with pytest.raises(PlanUsagePersistenceError):
        await codex_usage_calls.record_call_llm_usage(
            "codex-thread", _live(), provider_turn_id="turn-a", ordinal=1,
            measured=_row(input_uncached=100),
        )


@pytest.mark.asyncio
async def test_a_restarted_session_keys_its_readings_by_its_own_provider_turn(monkeypatch) -> None:
    """A session restart continues the Turn in a new provider turn, counting from one again."""
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(codex_usage_calls, "run_in_db", capture)
    live = _live()
    await codex_usage_calls.record_call_llm_usage(
        "codex-thread", live, provider_turn_id="turn-a", ordinal=1, measured=_row(input_uncached=100),
    )
    await codex_usage_calls.record_call_llm_usage(
        "codex-thread", live, provider_turn_id="turn-b", ordinal=1, measured=_row(input_uncached=100),
    )

    assert [c["usage_key"] for c in captured] == ["reading:turn-a:1", "reading:turn-b:1"]
    assert all(c["turn_id"] == "codex-turn" for c in captured)


@pytest.mark.asyncio
async def test_a_reading_the_ledger_cannot_take_is_the_turns_defect(monkeypatch) -> None:
    """No provider turn, or no model: nothing to receipt, but never only a warning."""
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"fn": fn, **kwargs})

    monkeypatch.setattr(codex_usage_calls, "run_in_db", capture)
    live = _live()
    await codex_usage_calls.record_call_llm_usage(
        "codex-thread", live, provider_turn_id=None, ordinal=1, measured=_row(input_uncached=100),
    )
    live.provider_reported_model = None
    await codex_usage_calls.record_call_llm_usage(
        "codex-thread", live, provider_turn_id="turn-a", ordinal=2, measured=_row(input_uncached=100),
    )

    assert [c["fn"] for c in captured] == [codex_usage_calls.report_unrecordable_call] * 2
    assert [(c["reason"], c["usage_key"]) for c in captured] == [
        ("provider_turn_unnamed", "reading:?:1"), ("model_unknown", "reading:turn-a:2"),
    ]


@pytest.mark.asyncio
async def test_a_terminal_without_a_model_is_the_turns_defect(monkeypatch) -> None:
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"fn": fn, **kwargs})

    monkeypatch.setattr(codex_runtime_terminal, "run_in_db", capture)
    live = _live()
    live.provider_reported_model = None

    assert await codex_runtime_terminal.record_terminal_llm_usage(
        "codex-thread", live, TokenUsage(input_uncached=250),
    ) == {}

    assert [(c["fn"], c["reason"], c["usage_key"]) for c in captured] == [
        (codex_runtime_terminal.report_unrecordable_call, "model_unknown", "terminal"),
    ]


@pytest.mark.asyncio
async def test_a_resumed_reading_without_a_reported_model_uses_the_selected_one(
    monkeypatch,
) -> None:
    """A ``thread/resume`` reply that omits ``model`` must not drop the reading.

    The catalog resolution already fixed ``expected_reported_model`` on
    ``live`` before this reading arrived; the receipt is recorded under it
    instead of being discarded as ``model_unknown``.
    """
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"fn": fn, "args": args, **kwargs})

    monkeypatch.setattr(codex_usage_calls, "run_in_db", capture)
    live = _live()
    live.provider_reported_model = None
    live.expected_reported_model = "gpt-5.6-sol"

    await codex_usage_calls.record_call_llm_usage(
        "codex-thread", live, provider_turn_id="turn-a", ordinal=1,
        measured=_row(input_uncached=100),
    )

    assert [c["fn"] for c in captured] == [codex_usage_calls.record_observed_call_usage]
    assert captured[0]["args"][1] == "codex/gpt-5.6-sol"
    assert captured[0]["usage_key"] == "reading:turn-a:1"


@pytest.mark.asyncio
async def test_a_resumed_terminal_without_a_reported_model_uses_the_selected_one(
    monkeypatch,
) -> None:
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append({"fn": fn, "args": args, **kwargs})

    monkeypatch.setattr(codex_runtime_terminal, "run_in_db", capture)
    live = _live()
    live.provider_reported_model = None
    live.expected_reported_model = "gpt-5.6-sol"

    result = await codex_runtime_terminal.record_terminal_llm_usage(
        "codex-thread", live, TokenUsage(input_uncached=250),
    )

    assert result == {}
    assert [c["fn"] for c in captured] == [codex_runtime_terminal.record_turn_fallback]
    assert captured[0]["args"][1] == "codex/gpt-5.6-sol"
    assert captured[0]["usage_key"] == "terminal"


@pytest.mark.asyncio
async def test_a_failed_turn_hands_the_ledger_its_delta(monkeypatch) -> None:
    """A refused receipt is covered by the failed terminal like a completed one."""
    calls: list[dict] = []

    async def capture(fn, *args, **kwargs):
        calls.append({"fn": fn, "args": args, **kwargs})

    monkeypatch.setattr(codex_runtime_terminal, "run_in_db", capture)
    await codex_runtime_terminal.settle_failed_turn_usage(
        "codex-thread", _live(), TokenUsage(input_uncached=250),
    )

    assert [c["usage_key"] for c in calls] == ["terminal"]
    assert calls[0]["fn"] is codex_runtime_terminal.record_turn_fallback
    assert calls[0]["args"][2]["prompt_tokens"] == 250
    assert calls[0]["quiet_if_unmeasured"] is True


@pytest.mark.asyncio
async def test_a_turn_that_failed_before_its_first_call_writes_no_usage(monkeypatch) -> None:
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(codex_usage_calls, "run_in_db", capture)
    monkeypatch.setattr(codex_runtime_terminal, "run_in_db", capture)
    live = _live()

    await codex_runtime_terminal.settle_failed_turn_usage("codex-thread", live, None)

    assert captured == []


@pytest.mark.asyncio
async def test_the_failed_terminal_settles_usage_before_its_marker(monkeypatch) -> None:
    from core.turn_core import TurnTerminalResult
    from infra.thread_inputs import TurnIdentity

    order: list[str] = []

    async def settle(thread_id, live, volume):
        order.append(f"settle:{volume.input_uncached}")
        return {}

    async def claim(turn_id, outcome, message):
        assert turn_id == "codex-turn"
        return SimpleNamespace(result=TurnTerminalResult.APPLIED)

    async def marker(thread_id, **kwargs):
        order.append("marker")
        return "marker-1"

    async def publish(**kwargs):
        order.append("publish")

    async def release(ctx, settlement):
        order.append("release")

    monkeypatch.setattr(codex_runtime_events, "_settle_failed_turn_usage", settle)
    monkeypatch.setattr(codex_runtime_events, "commit_turn_terminal", claim)
    monkeypatch.setattr(codex_runtime_events, "record_terminal_marker", marker)
    monkeypatch.setattr(codex_runtime_events, "_publish_claimed_terminal", publish)
    monkeypatch.setattr(codex_runtime_events, "release_and_drive_terminal", release)
    ctx = SimpleNamespace(
        thread_id="codex-thread", live=_live(), dead_set=set(), thread_registry={}, bcast=None,
    )
    ev = {
        "kind": "turn_failed", "cause": "process_exited",
        "token_volume": TokenUsage(input_uncached=250),
        "_corsia_turn_identity": TurnIdentity(turn_id="codex-turn", turn_order=1),
    }

    await codex_runtime_events._on_turn_failed(ctx, ev)

    assert order == ["settle:250", "marker", "publish", "release"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_error", "last_assistant_message_id", "expected_quiet"),
    [(True, None, True), (True, "assistant-output", False), (False, None, False)],
)
async def test_result_passes_quiet_usage_only_for_an_error_without_output(
    monkeypatch, is_error, last_assistant_message_id, expected_quiet,
) -> None:
    from core.turn_core import TurnTerminalResult
    from infra.thread_inputs import TurnIdentity

    captured: list[bool] = []

    async def claim(*_args, **_kwargs):
        return SimpleNamespace(result=TurnTerminalResult.APPLIED)

    async def record_usage(*_args, **kwargs):
        captured.append(kwargs["quiet_if_unmeasured"])
        return {}

    async def marker(*_args, **_kwargs):
        return "marker"

    async def publish(**_kwargs):
        return None

    async def release(*_args, **_kwargs):
        return None

    monkeypatch.setattr(codex_runtime_events, "commit_turn_terminal", claim)
    monkeypatch.setattr(codex_runtime_events, "_record_terminal_llm_usage", record_usage)
    monkeypatch.setattr(codex_runtime_events, "record_terminal_marker", marker)
    monkeypatch.setattr(codex_runtime_events, "_publish_claimed_terminal", publish)
    monkeypatch.setattr(codex_runtime_events, "record_external_turn_end", AsyncMock())
    monkeypatch.setattr(codex_runtime_events, "release_and_drive_terminal", release)
    live = _live()
    live.last_assistant_message_id = last_assistant_message_id
    ctx = SimpleNamespace(thread_id="codex-thread", live=live, bcast=lambda *_args: None)

    await codex_runtime_events._on_result(ctx, {
        "kind": "result",
        "is_error": is_error,
        "result": "provider failed",
        "_corsia_turn_identity": TurnIdentity(turn_id="codex-turn", turn_order=1),
    })

    assert captured == [expected_quiet]
