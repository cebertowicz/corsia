"""Memory-fit window for a local oMLX seat (#1219, provider half)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from llm import memory_fit  # noqa: E402
from llm.context_window import (  # noqa: E402
    get_context_window_or_none, resolve_live_window_facts, resolve_window_facts, resolve_window_for_row,
)

GIB = 2 ** 30
SID = "ns-memfit"
MODEL = "Qwen-Local"
SELECTOR = f"omlx@{SID}/{MODEL}"
GUARD = (
    "litellm.BadRequestError: OpenAIException - oMLX prefill memory guard rejected "
    "this prompt: Prefill would require ~20.80 GB peak (current 18.80 GB + KV+SDPA "
    "2.00 GB) but the prefill limit is 20.75 GB (metal_cap ceiling 22.56 GB - "
    "safety margin 1.80 GB)"
)
# Turn & Delivery's dev.917 pin: L 22.0, C 19.5, K 3.2 GB at P 33,270 fits 25,992
# tokens, under that seat's 29,075-token fixed prompt and tools.
PINNED = ("prefill memory guard rejected this prompt: (current 19.5 GB + KV+SDPA "
          "3.2 GB) but the prefill limit is 22.0 GB")
PINNED_FIT = 25_992
ROW = {"id": "omlx-row", "harness": "native", "provider": "omlx",
       "provider_model_id": MODEL, "litellm_model": SELECTOR, "context_window": 262_144}


@pytest.fixture(autouse=True)
def _clean():
    memory_fit.reset_for_tests()
    yield
    memory_fit.reset_for_tests()


@contextmanager
def _turn(turn_id: str | None):
    from infra.state.context import _current_turn_identity
    from infra.thread_input_types import TurnIdentity

    token = _current_turn_identity.set(TurnIdentity(turn_id, 1) if turn_id else None)
    try:
        yield
    finally:
        _current_turn_identity.reset(token)


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE model_profiles (model_id TEXT PRIMARY KEY, context_window)")
    return db


def _refuse(prompt_tokens: int = 100_000, *, turn_id: str = "refused", text: str = GUARD) -> None:
    with _turn(turn_id):
        memory_fit.record_refusal(SID, MODEL, text, prompt_tokens=prompt_tokens)


def _fit(prompt_tokens: int, text: str = GUARD) -> int:
    limit, current, kv = memory_fit.parse_guard_refusal(text)
    return int(prompt_tokens * (limit - current) / kv)


def _live():
    return resolve_live_window_facts(ROW, db=_db())


def test_parse_reads_limit_current_and_kv():
    assert memory_fit.parse_guard_refusal(GUARD) == (
        int(20.75 * GIB), int(18.80 * GIB), int(2.00 * GIB),
    )


def test_parse_reads_the_memory_monitor_wording():
    # oMLX memory_monitor.py: "... but <binding> ceiling is L (usage ..., ceiling ...)".
    text = (
        "oMLX prefill memory guard rejected this prompt: Prefill would require ~23.10 GB "
        "peak (current 21.10 GB + KV+SDPA 2.00 GB) but metal_cap ceiling is 22.56 GB "
        "(usage 21.1 GB, ceiling 22.6 GB). Raise kernel iogpu.wired_limit_mb."
    )
    assert memory_fit.parse_guard_refusal(text) == (
        int(22.56 * GIB), int(21.10 * GIB), int(2.00 * GIB),
    )


def test_parse_fails_safe_on_refusals_without_the_memory_terms():
    # The pre-chunk guard names no current or KV+SDPA term to learn from.
    assert memory_fit.parse_guard_refusal(
        "Prefill context too large for available memory (pre-chunk guard at 4096 tokens, "
        "kv_len=4096): predicted peak would exceed prefill safety cap 20.0GB"
    ) is None
    assert memory_fit.parse_guard_refusal("anything else") is None


def test_no_observation_leaves_the_window_untouched():
    assert (_live().effective, _live().source) == (262_144, "provider")


def test_refusal_caps_the_window_to_the_memory_fit_and_tags_it():
    _refuse(100_000)
    facts = _live()
    assert facts.effective == _fit(100_000)
    assert facts.discovered == 262_144
    assert facts.source == "memory"


def test_settings_and_catalog_resolvers_keep_the_configured_window():
    _refuse(100_000)
    assert tuple(resolve_window_facts(ROW, db=_db())) == (262_144, 262_144, "provider")
    assert resolve_window_for_row(ROW, db=_db()) == 262_144


def test_live_window_lookup_by_selector_is_capped_and_configured_is_not(monkeypatch):
    from llm import context_window

    _refuse(100_000)
    monkeypatch.setattr(context_window, "_catalog_row", lambda model, db=None: dict(ROW))
    assert get_context_window_or_none(SELECTOR, db=_db()) == _fit(100_000)
    assert get_context_window_or_none(SELECTOR, db=_db(), live=False) == 262_144


def test_window_never_exceeds_the_model_window():
    _refuse(1_000_000)
    assert _live().effective == 262_144


def test_window_is_one_token_when_no_headroom_remains():
    _refuse(100_000, text=GUARD.replace("current 18.80 GB", "current 21.00 GB"))
    assert _live().effective == 1


def test_cap_ends_with_the_turn_it_was_learned_in():
    _refuse(33_270, text=PINNED)
    assert _live().effective == PINNED_FIT
    memory_fit.end_turn("another-turn")
    assert _live().effective == PINNED_FIT
    memory_fit.end_turn("refused")
    assert _live().source == "provider"
    memory_fit.end_turn("refused")  # a retried terminal finds nothing left
    assert _live().source == "provider"


def test_a_newer_turns_cap_survives_the_older_turns_end():
    _refuse(100_000, turn_id="older")
    _refuse(33_270, turn_id="newer", text=PINNED)
    memory_fit.end_turn("older")
    assert _live().effective == PINNED_FIT


def test_a_refusal_outside_any_turn_records_nothing():
    # An idle or manual checkpoint fork has no Turn to end its cap.
    _refuse(100_000, turn_id=None)
    assert _live().source == "provider"
    _refuse(100_000)
    assert _live().source == "memory"


def test_non_omlx_rows_are_never_capped():
    _refuse()
    row = {**ROW, "litellm_model": "openai/gpt-x", "provider": "openai"}
    assert resolve_live_window_facts(row, db=_db()).source == "provider"


def test_unparseable_refusal_records_nothing():
    _refuse(10, text="metal_cap ceiling is 230.12 GB")
    assert _live().source == "provider"


def test_overflow_disposition_records_the_observation_for_a_local_model():
    from llm.provider_overflow import OverflowDisposition, overflow_disposition

    messages = [{"role": "user", "content": "word " * 2000}]
    with _turn("refused"):
        disposition = asyncio.run(overflow_disposition(
            Exception(GUARD), f"openai/{MODEL}", messages=messages, already_retried=True,
            omlx_server_id=SID,
        ))
    assert disposition is OverflowDisposition.HANDLED
    assert _live().source == "memory"


def test_execution_target_carries_the_capped_window(monkeypatch):
    from llm import resolution

    _refuse(100_000)
    monkeypatch.setattr(resolution, "_target_row", lambda model, db=None: dict(ROW, metadata_json="{}", enabled=1, available=1))
    db = _db()
    db.execute("CREATE TABLE threads (id TEXT, user_id TEXT, project_id TEXT, local_only INTEGER)")
    db.execute("INSERT INTO threads VALUES ('t', 'u', NULL, 0)")
    target = resolution.resolve_locality_permitted(SELECTOR, thread_id="t", db=db)
    assert target.context_window == _fit(100_000)
    assert target.context_window_source == "memory"


def test_finish_turn_ends_the_cap_its_turn_learned():
    from infra.thread_driver import finish_turn
    from tests.test_clean_turn_terminal import _db as schema_db, _running_turn, _session

    db = schema_db()
    try:
        _session(db, "thread-1")
        db.commit()
        _running_turn(db, "thread-1", "turn-1")
        _refuse(33_270, turn_id="turn-1", text=PINNED)
        assert _live().effective == PINNED_FIT
        finish_turn(
            "turn-1", outcome="error", ended_at="t3", successor_turn_id="turn-2",
            boot_epoch="boot-1", error="refused", db=db,
        )
    finally:
        db.close()
    assert _live().source == "provider"


async def _native_boundary(thread_id: str, turn_id: str, monkeypatch):
    """Run the real Native boundary, its run_in_db hop included, and capture it."""
    from core import checkpoint_resume, turn_iteration
    from core import turn_iteration_context as iteration_context
    from core.checkpoint_resume import CheckpointWaitScope, ProviderContinuation
    from core.fork_fit import BoundaryDecision
    from core.turn_core import TurnIdentity

    seen: dict = {}

    async def maintain(boundary, *, must_adopt=False):
        seen["boundary"] = boundary
        return ProviderContinuation(BoundaryDecision.PROCEED, boundary.turn)

    monkeypatch.setattr(checkpoint_resume, "maintain_before_provider_call", maintain)
    window = resolve_live_window_facts(dict(ROW, context_window=32_768)).effective
    ledger = SimpleNamespace(
        target_catalog_id=SELECTOR, effective_window_tokens=window, rendered_input_tokens=21_000,
        input_identity="i", section_tokens=(("fixed", 14_000), ("current_input", 2_000),
                                            ("tools", 4_618), ("provider_wrapper", 0)), fits=True,
    )
    state = turn_iteration._IterationState(
        messages=[{"role": "user", "content": "go"}],
        context_usage={"fit_section_tokens": {"fixed": 14_000, "tools": 4_618}},
        tool_definitions=[], context_identity=None, fit_ledger=ledger, fit_current_input="go",
        checkpoint_source_messages=[], turn_model=SELECTOR,
    )
    with _turn(turn_id):
        await iteration_context.maintain_native_checkpoint(
            agent=None, state=state, thread_id=thread_id, turn_identity=TurnIdentity(turn_id, 1),
            cancel_event=SimpleNamespace(checkpoint_wait_scope=CheckpointWaitScope()),
            turn_model=SELECTOR, reasoning_effort=None, project_id=None,
        )
    return seen["boundary"]


@pytest.mark.asyncio
async def test_the_native_boundary_sizes_at_the_cap_and_settles_on_the_configured_window(
    isolated_db, monkeypatch,
):
    from infra.database import get_db
    from infra.checkpoint_refusal_settlement import _recovery_changed
    from tests.unavailable_model_support import seed_model

    seed_model(SELECTOR)  # configured window 32,768
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at)"
            " VALUES ('t-mem','user-1',?,'t0','t0')", (SELECTOR,),
        )
        db.commit()
    finally:
        db.close()
    _refuse(33_270, turn_id="refused", text=PINNED)

    capped = await _native_boundary("t-mem", "refused", monkeypatch)
    refusal = capped.unavailable("checkpoint plus tail does not fit the selected provider")

    assert capped.context_window == PINNED_FIT
    assert refusal.context_window == 32_768
    memory_fit.end_turn("refused")
    rows = [{"thread_id": "t-mem", "target_model": SELECTOR}]
    db = get_db()
    try:
        # The cap ending with its Turn is no recovery: the inputs fail visibly
        # once instead of being released into the same refusal again.
        assert _recovery_changed(db, rows, refusal) is False
    finally:
        db.close()
    assert (await _native_boundary("t-mem", "next", monkeypatch)).configured_window is None
