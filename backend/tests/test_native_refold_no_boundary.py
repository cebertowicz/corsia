"""A required re-fold with no history boundary newer than the installed floor.

The 917 shape: a 41,681-token local seat, the first-run fixed prompt plus tools,
one Turn reading about 61k characters, and a checkpoint already installed just
before that Turn's newest Assistant Step. A prefill refusal teaches a smaller
memory-fit window (#1929); the Turn's one retry must adopt a newer checkpoint
(#1924), but nothing newer than the floor exists to fold.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.checkpoint_resume import CheckpointUnavailable, CheckpointWaitScope
from core.turn_core import TurnIdentity

SKILL = Path(__file__).resolve().parents[1] / "prompts/system/v1/skills/conductor-bootstrap"
MODEL = "oMLX/qwen"
SEAT = {"litellm_model": "omlx@srv/qwen"}
WINDOW = 41_681
# The first-run fixed prompt and tool schemas: about 84k characters, the ~28k
# tokens the 917 setup turn carried by the characters/3 estimate.
FIXED_CHARS, TOOL_COUNT, TOOL_CHARS = 60_000, 24, 1_000
READS = (  # the setup turn's reads, 61,107 characters
    ("references/first-run-start.md", 1_456),
    ("references/first-run-app.md", 28_726),
    ("references/first-run.md", 18_368),
    ("references/look-up-the-person.md", 14_557),
)
REASONING = "I should read the next setup reference before replying. " * 40
# Constructed oMLX scheduler guard text; L, current and KV+SDPA are in GB.
REFUSAL = (
    "Prompt rejected: the prefill limit is 22.0 GB, but current 19.5 GB "
    "+ KV+SDPA 3.2 GB would exceed it."
)


def _text(name: str, size: int) -> str:
    body = (SKILL / name).read_text()
    while len(body) < size:
        body += "\n" + body
    return body[:size]


FIXED = _text("SKILL.md", FIXED_CHARS)
TOOLS = [
    {"type": "function", "function": {
        "name": f"tool_{index}", "description": _text("SKILL.md", TOOL_CHARS),
        "parameters": {"type": "object", "properties": {}},
    }}
    for index in range(TOOL_COUNT)
]


def _seed_setup_turn(thread: str) -> str:
    """The setup Turn with a checkpoint installed before its newest Step."""
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?,'u',?,'t','t')",
        (thread, MODEL),
    )
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('turn-1',?,0,?,'running','t','boot')", (thread, MODEL),
    )
    rows: list[tuple[str, str, str, dict[str, object]]] = [
        ("u1", "user", "Yes, please continue with my setup.", {"turn_id": "turn-1"}),
    ]
    for index, (name, size) in enumerate(READS, 1):
        rows.append((f"s{index}", "assistant", "", {
            "turn_id": "turn-1", "reasoning": REASONING,
            "llm_tool_calls": [{"id": f"c{index}", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": name})}}],
        }))
        rows.append((f"r{index}", "tool", _text(name, size), {
            "turn_id": "turn-1", "tool_call_id": f"c{index}",
            "assistant_step_id": f"{thread}-s{index}", "name": "read_file",
        }))
    db.executemany(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) VALUES (?,?,?,?,?,'t',?)",
        [(f"{thread}-{m}", thread, r, c, json.dumps(md), seq) for seq, (m, r, c, md) in enumerate(rows, 1)],
    )
    floor_seq = len(rows) - 2  # the last read before the newest Step
    checkpoint_id = f"{thread}-c1"
    db.execute(
        "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,through_message_id,trigger,"
        " mode,status,source_harness,source_model,writer_harness,writer_model,"
        " continuity_base_revision,continuity_base_snapshot_json,started_at,raw_result,"
        " continuity_snapshot_json,captured_at,installed_at) "
        "VALUES (?,?,0,?,?,'pressure','cold','installed','native',?,'native',?,0,'[]','t',?,'[]','t','t')",
        (checkpoint_id, thread, floor_seq, f"{thread}-r{len(READS) - 1}", MODEL, MODEL,
         "The person is partway through first-run setup; three references read."),
    )
    db.commit()
    db.close()
    return checkpoint_id


def _further_send(thread: str, state):
    """The person's next message in the same chat, as a new Turn."""
    from infra.database import get_db

    db = get_db()
    db.execute("UPDATE active_turns SET status='error' WHERE id='turn-1'")
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('turn-2',?,0,?,'running','t','boot')", (thread, MODEL),
    )
    seq = db.execute("SELECT MAX(seq) FROM messages WHERE thread_id=?", (thread,)).fetchone()[0]
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,'user','hello?',?,'t',?)",
        (f"{thread}-u2", thread, json.dumps({"turn_id": "turn-2"}), seq + 1),
    )
    db.commit()
    db.close()
    message = {"role": "user", "content": "hello?"}
    return _state([*state.messages, message], usage=state.context_usage)


@pytest.fixture
def seat(isolated_db, monkeypatch):
    """One loaded oMLX seat whose window is the configured one capped by memory fit."""
    from core import turn_iteration_context as iteration_context
    from llm import memory_fit

    monkeypatch.setattr("llm.network_discovery.get_server", lambda _s: {"memory_used_mb": 20_000})
    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _s: {"qwen"})
    monkeypatch.setattr("llm.server_observation.observation_generation", lambda _s: 1)

    def live_window(_selector, *, thread_id):
        # context_window.resolve_live_window_facts: configured, capped by memory fit.
        fit = memory_fit.memory_fit_window(SEAT)
        return SimpleNamespace(
            context_window=min(WINDOW, fit or WINDOW),
            expected_reported_model=None, transport_model=MODEL,
        )

    monkeypatch.setattr("llm.resolution.resolve_locality_permitted", live_window)

    async def prefix(*, thread_id, turn_model, replay_selection, **_kwargs):
        from core.continuity_accounting import ContinuityAccounting
        from core.native_context_prefix import _load_history

        history = _load_history(
            thread_id=thread_id, turn_model=turn_model, fresh_context=False,
            replay_selection=replay_selection,
        )
        return [{"role": "system", "content": FIXED}, *history], {
            "continuity_accounting": ContinuityAccounting.not_delivered(),
        }

    monkeypatch.setattr("core.native_context_prefix.build_real_send_prefix", prefix)
    monkeypatch.setattr(iteration_context, "_retain_continuation", _noop)
    yield
    memory_fit.reset_for_tests()


async def _noop(*_args, **_kwargs):
    return None


def _state(messages, *, usage, window=WINDOW):
    from core.destination_preflight import attach_native_provider_fit_ledger
    from core.turn_iteration import _IterationState

    ledger = attach_native_provider_fit_ledger(
        target_catalog_id=MODEL, proven_target_window_tokens=window, messages=messages,
        tools=TOOLS, input_identity="input-1", current_input=messages[-1], model=MODEL,
    ).fit_ledger
    return _IterationState(
        messages=list(messages), context_usage=dict(usage), tool_definitions=list(TOOLS),
        context_identity=None, fit_ledger=ledger, fit_current_input=messages[-1],
        checkpoint_source_messages=[], turn_model=MODEL, provider_delivery_settled=True,
    )


async def _adopted_state(thread: str, checkpoint_id: str):
    """The Turn as it stands once it adopted the installed checkpoint."""
    from core.turn_iteration_context import _render_native_installed
    from infra.checkpoint_queries import read

    state = _state([{"role": "user", "content": "placeholder"}], usage={})
    installed = await _render_native_installed(
        read(checkpoint_id), agent=None, state=state, thread_id=thread,
        turn_identity=TurnIdentity("turn-1", 1), turn_model=MODEL, project_id=None,
    )
    payload = installed.payload
    return _state(list(payload.messages), usage=payload.context_usage), installed.input_tokens


async def _boundary(state, thread: str, turn: str = "turn-1"):
    from core.turn_iteration_context import maintain_native_checkpoint

    return await maintain_native_checkpoint(
        agent=None, state=state, thread_id=thread, turn_identity=TurnIdentity(turn, 1),
        cancel_event=SimpleNamespace(checkpoint_wait_scope=CheckpointWaitScope()),
        turn_model=MODEL, reasoning_effort=None, project_id=None,
    )


async def _refuse(state) -> None:
    """The provider's prefill refusal, through the dispatch and Turn paths that learn it."""
    from core.turn_provider_error import check_provider_error
    from llm.provider_overflow import _learn_memory_fit
    from llm.router import LLMResponse

    _learn_memory_fit(RuntimeError(REFUSAL), "openai/qwen", state.messages, TOOLS, "srv")
    response = LLMResponse(REFUSAL, finish_reason="error", error_kind="provider_context_overflow")
    assert await check_provider_error(state, response, MODEL, "t", _noop, True)
    assert state.overflow_retry_pending


async def _outcome(state, thread: str, turn: str) -> str:
    try:
        await _boundary(state, thread, turn)
    except CheckpointUnavailable as error:
        return str(error)
    return "replies"


# The capped window (25,600) is under the fixed prompt plus tools (29,075), and
# nothing newer than the installed floor exists to fold, so the installed
# checkpoint plus its tail (33,175) is what cannot fit.
TAIL_TOO_LARGE = "checkpoint plus tail does not fit the selected provider"


def _count_reads(monkeypatch) -> list[str]:
    """Record each fold-source read, the history render the boundary pays for."""
    from core import turn_iteration_context as iteration_context

    reads: list[str] = []
    read = iteration_context._read_native_checkpoint_context

    def counted(thread_id, target_catalog_id):
        reads.append(thread_id)
        return read(thread_id, target_catalog_id)

    monkeypatch.setattr(iteration_context, "_read_native_checkpoint_context", counted)
    return reads


def _new_chat(thread: str):
    """A new chat's first send: one user message and no history to fold."""
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?,'u',?,'t','t')",
        (thread, MODEL),
    )
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('turn-3',?,0,?,'running','t','boot')", (thread, MODEL),
    )
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,'user','hi',?,'t',1)",
        (f"{thread}-u1", thread, json.dumps({"turn_id": "turn-3"})),
    )
    db.commit()
    db.close()
    return _state([{"role": "system", "content": FIXED}, {"role": "user", "content": "hi"}], usage={})


async def _after_the_cap_clears(state, monkeypatch) -> list[str]:
    """The next send in the same chat, and a new chat, once #1929's cap is gone."""
    from llm import memory_fit

    async def no_fold(*_args, **_kwargs):
        return None  # an optional fold starts nothing here, so no provider is called

    monkeypatch.setattr("core.checkpoint_runtime.request_checkpoint", no_fold)
    memory_fit.reset_for_tests()
    same_chat = await _outcome(_further_send("t1", state), "t1", "turn-2")
    new_chat = await _outcome(_new_chat("t2"), "t2", "turn-3")
    return [same_chat, new_chat]


@pytest.mark.asyncio
async def test_refused_retry_with_nothing_newer_to_fold_ends_handled_without_a_second_call(
    seat, monkeypatch,
):
    """(B) The retry adopts nothing, so the Turn ends on the refusal, handled."""
    from core.turn_provider_error import TurnProviderError, end_if_overflow_unchanged

    checkpoint_id = _seed_setup_turn("t1")
    state, _installed_tokens = await _adopted_state("t1", checkpoint_id)
    assert state.context_usage["checkpoint_generation_id"] == checkpoint_id
    await _refuse(state)

    prepared = await _boundary(state, "t1", "turn-1")
    steps: list[str] = []

    async def on_step(kind, _payload):
        steps.append(kind)

    with pytest.raises(TurnProviderError, match="Retry the send"):
        await end_if_overflow_unchanged(state, prepared, MODEL, on_step)

    assert prepared is None and steps == ["error"]
    assert await _after_the_cap_clears(state, monkeypatch) == ["replies", "replies"]


@pytest.mark.asyncio
async def test_capped_installed_tail_with_nothing_newer_to_fold_reports_the_tail(seat, monkeypatch):
    """(A) The installed checkpoint plus its tail is over the capped window."""
    from llm.memory_fit import memory_fit_window

    checkpoint_id = _seed_setup_turn("t1")
    state, installed_tokens = await _adopted_state("t1", checkpoint_id)
    await _refuse(state)
    state.overflow_retry_pending = False  # the window stays capped without the retry
    assert installed_tokens > memory_fit_window(SEAT)
    reads = _count_reads(monkeypatch)

    assert await _outcome(state, "t1", "turn-1") == TAIL_TOO_LARGE
    assert reads == ["t1"]  # one render, from the required start's own read
    assert await _outcome(_further_send("t1", state), "t1", "turn-2") == TAIL_TOO_LARGE


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow_retry", [True, False])
async def test_a_target_that_cannot_fold_still_reports_admission_unavailable(
    seat, monkeypatch, overflow_retry,
):
    """A switched or unavailable target is not nothing-newer: its cause is unchanged."""
    from llm import resolution

    checkpoint_id = _seed_setup_turn("t1")
    state, _installed_tokens = await _adopted_state("t1", checkpoint_id)
    await _refuse(state)
    state.overflow_retry_pending = overflow_retry
    monkeypatch.setattr(
        resolution, "resolve_thread_target",
        lambda *_a, **_k: SimpleNamespace(catalog_id=MODEL, harness="native", available=False),
    )

    assert await _outcome(state, "t1", "turn-1") == "checkpoint admission is unavailable"
