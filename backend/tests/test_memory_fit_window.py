"""Memory-fit window for a local oMLX seat (#1219, provider half)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from llm import memory_fit, network_discovery, server_observation  # noqa: E402
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
ROW = {"id": "omlx-row", "harness": "native", "provider": "omlx",
       "provider_model_id": MODEL, "litellm_model": SELECTOR, "context_window": 262_144}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    memory_fit.reset_for_tests()
    monkeypatch.setattr(network_discovery, "_LOADED_BY_SERVER", {SID: {MODEL}})
    monkeypatch.setattr(server_observation, "_GENERATION", {})
    _USED_MB.clear()
    monkeypatch.setattr(
        network_discovery, "get_server",
        lambda sid: {"id": sid, "memory_used_mb": _USED_MB.get("mb")},
    )
    yield
    memory_fit.reset_for_tests()


_USED_MB: dict[str, int] = {}


def _used(mb: int) -> None:
    _USED_MB["mb"] = mb


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE model_profiles (model_id TEXT PRIMARY KEY, context_window)")
    return db


def _refuse(prompt_tokens: int = 100_000) -> None:
    memory_fit.record_refusal(SID, MODEL, GUARD, prompt_tokens=prompt_tokens)


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
    facts = resolve_live_window_facts(ROW, db=_db())
    assert (facts.effective, facts.source) == (262_144, "provider")


def test_refusal_caps_the_window_to_the_memory_fit_and_tags_it():
    _refuse(100_000)
    facts = resolve_live_window_facts(ROW, db=_db())
    # 100_000 * (20.75 - 18.80) / 2.00 = 97_500 -> rounded down to 1024s
    assert facts.effective == 97_500 // 1024 * 1024
    assert facts.discovered == 262_144
    assert facts.source == "memory"


def test_settings_and_catalog_resolvers_keep_the_configured_window():
    _refuse(100_000)
    assert tuple(resolve_window_facts(ROW, db=_db())) == (262_144, 262_144, "provider")
    assert resolve_window_for_row(ROW, db=_db()) == 262_144


def test_live_window_lookup_by_selector_is_capped(monkeypatch):
    from llm import context_window

    _refuse(100_000)
    monkeypatch.setattr(context_window, "_catalog_row", lambda model, db=None: dict(ROW))
    assert get_context_window_or_none(SELECTOR, db=_db()) == 97_500 // 1024 * 1024


def test_window_never_exceeds_the_model_window():
    memory_fit.record_refusal(SID, MODEL, GUARD, prompt_tokens=1_000_000)
    assert resolve_live_window_facts(ROW, db=_db()).effective == 262_144


def test_window_is_stable_between_reads_with_slightly_different_headroom():
    _used(19 * 1024)
    _refuse(101_350)  # fit lands mid-step, so a few MB of headroom cannot cross a step
    _used(19 * 1024 + 3)
    first = resolve_live_window_facts(ROW, db=_db()).effective
    _used(19 * 1024 + 6)
    assert resolve_live_window_facts(ROW, db=_db()).effective == first
    assert first % 1024 == 0


def test_window_relaxes_when_model_memory_frees():
    _used(19 * 1024)
    _refuse(100_000)
    tight = resolve_live_window_facts(ROW, db=_db()).effective
    _used(18 * 1024)
    assert resolve_live_window_facts(ROW, db=_db()).effective > tight


def test_window_is_the_smallest_positive_granule_when_no_headroom_remains():
    _used(10 * 1024)
    _refuse(100_000)
    _used(30 * 1024)
    assert resolve_live_window_facts(ROW, db=_db()).effective == 1024


def test_observation_is_dropped_when_the_server_generation_changes():
    _refuse()
    server_observation._new_generation(SID)
    assert resolve_live_window_facts(ROW, db=_db()).source == "provider"
    server_observation._GENERATION.clear()  # still dropped, not resurrected
    assert resolve_live_window_facts(ROW, db=_db()).source == "provider"


def test_observation_is_dropped_when_the_model_is_no_longer_loaded(monkeypatch):
    _refuse()
    monkeypatch.setattr(network_discovery, "_LOADED_BY_SERVER", {SID: set()})
    assert resolve_live_window_facts(ROW, db=_db()).source == "provider"


def test_non_omlx_rows_are_never_capped():
    _refuse()
    row = {**ROW, "litellm_model": "openai/gpt-x", "provider": "openai"}
    assert resolve_live_window_facts(row, db=_db()).source == "provider"


def test_unparseable_refusal_records_nothing():
    memory_fit.record_refusal(SID, MODEL, "metal_cap ceiling is 230.12 GB", prompt_tokens=10)
    assert resolve_live_window_facts(ROW, db=_db()).source == "provider"


def test_overflow_disposition_records_the_observation_for_a_local_model():
    from llm.provider_overflow import OverflowDisposition, overflow_disposition

    messages = [{"role": "user", "content": "word " * 2000}]
    disposition = asyncio.run(overflow_disposition(
        Exception(GUARD), f"openai/{MODEL}", messages=messages, already_retried=True,
        omlx_server_id=SID,
    ))
    assert disposition is OverflowDisposition.HANDLED
    assert resolve_live_window_facts(ROW, db=_db()).source == "memory"


def test_execution_target_carries_the_capped_window(monkeypatch):
    from llm import resolution

    _refuse(100_000)
    monkeypatch.setattr(resolution, "_target_row", lambda model, db=None: dict(ROW, metadata_json="{}", enabled=1, available=1))
    db = _db()
    db.execute("CREATE TABLE threads (id TEXT, user_id TEXT, project_id TEXT, local_only INTEGER)")
    db.execute("INSERT INTO threads VALUES ('t', 'u', NULL, 0)")
    target = resolution.resolve_locality_permitted(SELECTOR, thread_id="t", db=db)
    assert target.context_window == 97_500 // 1024 * 1024
    assert target.context_window_source == "memory"
