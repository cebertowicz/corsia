"""A native Turn's successful terminal hands its final call's receipt to idle."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

PREFIX = [
    {"role": "system", "content": "Live native prefix."},
    {"role": "user", "content": "The Turn's question."},
]


def _state(**overrides):
    state = SimpleNamespace(
        messages=list(PREFIX),
        checkpoint_source_messages=list(PREFIX),
        tool_definitions=[{"type": "function", "function": {"name": "read_file"}}],
        last_measured_call_usage={"prompt_tokens": 60_000},
        last_measured_rendered_input_tokens=58_000,
        fit_ledger=SimpleNamespace(rendered_input_tokens=58_000),
        final_content="The Turn's answer.",
        context_usage={"checkpoint_generation_id": "gen-1"},
    )
    for name, value in overrides.items():
        setattr(state, name, value)
    return state


def test_a_turn_that_ends_on_its_measured_answer_freezes_a_receipt() -> None:
    from core.checkpoint_native_admission import freeze_idle_receipt

    receipt = freeze_idle_receipt(_state())

    assert receipt is not None
    assert receipt.messages == (
        *PREFIX, {"role": "assistant", "content": "The Turn's answer."},
    )
    assert receipt.tools == ({"type": "function", "function": {"name": "read_file"}},)
    assert (receipt.provider_input_tokens, receipt.rendered_input_tokens) == (
        60_000, 58_000,
    )
    # The installed checkpoint its input was built on, frozen at the terminal.
    assert receipt.generation_id == "gen-1"
    assert receipt.final_message_id is None


@pytest.mark.parametrize(
    "overrides",
    [
        # The final call reported no usage: the measurement is an older call's,
        # taken before a tool phase refit the ledger.
        {"fit_ledger": SimpleNamespace(rendered_input_tokens=61_000)},
        # No call was ever measured (an errored or cancelled only call).
        {"last_measured_call_usage": None},
        # The iteration cap ended on a tool step: its source is the shorter
        # resumable slice, not the whole live list.
        {"messages": [*PREFIX, {"role": "tool", "content": "result"}]},
        # An empty answer is an error terminal, never a completed one.
        {"final_content": None},
        {"fit_ledger": None},
    ],
    ids=["unmeasured-final-call", "no-measurement", "tool-step-end", "no-answer", "no-ledger"],
)
def test_no_receipt_unless_the_final_call_is_the_measured_answer(overrides) -> None:
    from core.checkpoint_native_admission import freeze_idle_receipt

    assert freeze_idle_receipt(_state(**overrides)) is None


def _turn(receipt, step_ids):
    return SimpleNamespace(idle_receipt=receipt, handler=SimpleNamespace(step_ids=step_ids))


@pytest.mark.parametrize(
    ("receipt", "step_ids", "marked"),
    [(None, ["answer-row"], True), ("receipt", [], True), ("receipt", ["answer-row"], False)],
    ids=["no-receipt", "no-answer-row", "no-marker"],
)
def test_terminal_leaves_the_settlement_alone_without_all_three(
    receipt, step_ids, marked,
) -> None:
    from core.checkpoint_native_admission import freeze_idle_receipt
    from core.native_turn_runtime import _with_idle_receipt
    from infra.idle_checkpoints import IdleCheckpointDue
    from infra.thread_driver import TerminalResult, TerminalSettlement

    due = IdleCheckpointDue("turn-1", 1, "thread-1", "2026-01-01T00:00:00")
    settlement = TerminalSettlement(
        result=TerminalResult.APPLIED, thread_id="thread-1",
        idle_checkpoint=due if marked else None,
    )
    frozen = freeze_idle_receipt(_state()) if receipt else None

    assert _with_idle_receipt(_turn(frozen, step_ids), settlement) is settlement


class _Agent:
    max_iterations = 5

    @staticmethod
    def _strip_think(content):
        return content


class _Handler:
    """Records what the Turn publishes; ``text`` persists the answer row."""

    def __init__(self) -> None:
        self.step_ids: list[str] = []
        self.published: list[tuple[str, dict]] = []

    async def handle(self, kind: str, payload: dict):
        self.published.append((kind, payload))
        if kind == "text":
            self.step_ids.append("answer-row")
            return "answer-row"
        return None


@pytest.mark.asyncio
async def test_a_real_native_turn_hands_its_receipt_to_the_idle_marker(monkeypatch) -> None:
    """Real state, final answer, freeze, terminal and hand-off: no stand-ins.

    Only the provider network call, the checkpoint maintenance boundary and
    the usage ledger writes are replaced.
    """
    from core import native_turn_runtime, turn_iteration, turn_provider_call
    from core.checkpoint_native_admission import IDLE_RECEIPT_KEY
    from core.destination_preflight import attach_native_provider_fit_ledger
    from core.native_turn_control import TurnCancelEvent
    from core.native_turn_runtime import NativeTurn, _run_native_turn, _with_idle_receipt
    from core.turn_core import TurnIdentity
    from core.turn_iteration import _IterationState
    from core.turn_iteration_context import current_fit_input
    from infra.idle_checkpoints import IdleCheckpointDue
    from infra.thread_driver import TerminalResult, TerminalSettlement
    from llm.router import LLMResponse

    messages = [dict(message) for message in PREFIX]
    ledger = attach_native_provider_fit_ledger(
        target_catalog_id="native/test", proven_target_window_tokens=200_000,
        messages=messages, tools=[], input_identity="input-1",
        current_input=current_fit_input(messages),
    ).fit_ledger
    state = _IterationState(
        messages=messages, context_usage={"checkpoint_generation_id": "gen-1"},
        tool_definitions=[], context_identity=None, fit_ledger=ledger,
        fit_current_input=current_fit_input(messages),
        checkpoint_source_messages=[], turn_model="native/test",
    )
    identity = TurnIdentity("turn-1", 1)

    async def call_llm(*_args, **_kwargs):
        return LLMResponse(content="The Turn's answer.", usage={"prompt_tokens": 60_000}), False

    async def no_fold(**_kwargs):
        return None

    async def no_db(*_args, **_kwargs):
        return None

    async def run_turn(*, agent, thread_id, on_step, cancel_event, model, **_kwargs):
        await turn_iteration._run_loop(
            agent, state, thread_id, model, {}, cancel_event, on_step,
            None, None, None, identity, None,
        )

    monkeypatch.setattr(turn_provider_call, "call_llm", call_llm)
    monkeypatch.setattr(turn_iteration.iteration_context, "maintain_native_checkpoint", no_fold)
    monkeypatch.setattr(turn_iteration, "run_in_db", no_db)
    monkeypatch.setattr(native_turn_runtime, "run_turn_with_steps", run_turn)
    cancel_event = TurnCancelEvent()
    cancel_event.bind_turn_identity(identity)
    turn = NativeTurn(
        agent=_Agent(), thread_id="thread-1", content="The Turn's question.",
        model="native/test", turn_provider={}, handler=_Handler(),
        cancel_event=cancel_event, identity=identity,
    )
    effects: dict[str, tuple[str, dict]] = {}

    assert await _run_native_turn(turn, effects) == "completed"
    assert IDLE_RECEIPT_KEY not in effects["turn_complete"][1]

    due = IdleCheckpointDue("turn-1", 1, "thread-1", "2026-01-01T00:00:00")
    handed = _with_idle_receipt(turn, TerminalSettlement(
        result=TerminalResult.APPLIED, thread_id="thread-1", idle_checkpoint=due,
    ))

    receipt = handed.idle_checkpoint.native_receipt
    assert receipt.final_message_id == "answer-row"
    assert receipt.generation_id == "gen-1"
    assert receipt.messages == (*PREFIX, {"role": "assistant", "content": "The Turn's answer."})
    assert receipt.rendered_input_tokens == ledger.rendered_input_tokens
    assert receipt.provider_input_tokens == 60_000
