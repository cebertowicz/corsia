"""Each ledger row records the effort its call ran with; each window change is kept.

Pins: the ``llm_usage.reasoning_effort`` column on the native direct path, the
native Turn path, the Claude Code and Codex per-call and terminal paths (the
effort comes from the live session, not the request), NULL when unknown; the
append-only ``limit_observations`` history (appended on a change, silent on an
unchanged reading); and the 97 -> 98 migration on an existing database.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from types import SimpleNamespace

import pytest

from connections import harness_usage
from core.turn_core import TurnIdentity
from core.turns_util import usage_call_fields
from external_harness_usage_helpers import persist_turn
from infra.database import get_db, initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.llm_usage import record_llm_usage
from infra.migration_97_to_98_effort_and_limit_history import migrate_97_to_98
from infra.schema import SCHEMA_VERSION
from plans import usage_calls
from subagents import cc_event_usage, codex_usage_calls
from subagents.cc_runtime_state import _Live
from subagents.codex_runtime_state import _LiveCodex
from subagents.usage_model import usage_effort
from vendor_account_fixtures import default_receipt

USAGE = {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14, "cost_usd": None}


def _efforts() -> dict[str, str | None]:
    db = get_db()
    try:
        return {r["id"]: r["reasoning_effort"] for r in db.execute(
            "SELECT id, reasoning_effort FROM llm_usage")}
    finally:
        db.close()


def _only_effort() -> str | None:
    values = list(_efforts().values())
    assert len(values) == 1
    return values[0]


def test_native_direct_call_records_the_effort_it_ran_with(isolated_db) -> None:
    record_llm_usage("t-native", "model-a", USAGE, source="conversation",
                     reasoning_effort="xhigh", _emit_product_event=lambda *a, **k: None)
    assert _only_effort() == "xhigh"


def test_a_call_that_cannot_know_its_effort_records_null(isolated_db) -> None:
    record_llm_usage("t-native", "model-a", USAGE, source="conversation",
                     _emit_product_event=lambda *a, **k: None)
    assert _only_effort() is None


def test_native_turn_call_records_the_effort_through_its_receipt(isolated_db) -> None:
    persist_turn("t-turn", "turn-1")
    fields = usage_call_fields(SimpleNamespace(ttft_ms=None), 1, None, "high")
    assert fields["reasoning_effort"] == "high"
    usage_calls.record_call_usage(
        "t-turn", "model-a", USAGE, source="conversation", turn_id="turn-1",
        usage_key=fields["usage_key"], binding=None, reasoning_effort=fields["reasoning_effort"],
        _emit_product_event=lambda *a, **k: None,
    )
    assert _only_effort() == "high"


def test_claude_code_call_records_the_session_effort_not_the_requested_one(isolated_db) -> None:
    persist_turn("cc-thread", "cc-turn")
    live = _Live(session=SimpleNamespace(effort=" max "), parent_thread_id=None, name="cc", model="opus")
    live.provider_reported_model = "opus"
    live.corsia_turn = TurnIdentity("cc-turn", 1)
    assert usage_effort(live) == "max"
    usage_calls.record_observed_call_usage(
        "cc-thread", "opus", USAGE, source="external_harness:claude-code",
        turn_id="cc-turn", usage_key="message:m1", reasoning_effort=usage_effort(live),
    )
    assert _only_effort() == "max"


def test_the_terminal_fallback_carries_the_same_effort(isolated_db) -> None:
    persist_turn("cc-thread", "cc-turn")
    usage_calls.record_turn_fallback(
        "cc-thread", "opus", USAGE, source="external_harness:claude-code",
        turn_id="cc-turn", reasoning_effort="low",
    )
    assert _only_effort() == "low"


@pytest.mark.asyncio
async def test_every_harness_writer_hands_the_ledger_its_session_effort(monkeypatch) -> None:
    captured: list[dict] = []

    async def capture(fn, *args, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(cc_event_usage, "run_in_db", capture)
    monkeypatch.setattr(codex_usage_calls, "run_in_db", capture)
    cc = _Live(session=SimpleNamespace(effort="medium"), parent_thread_id=None, name="cc", model="opus")
    cc.provider_reported_model = "opus"
    cc.corsia_turn = TurnIdentity("cc-turn", 1)
    codex = _LiveCodex(session=SimpleNamespace(effort="high"), parent_thread_id=None, name="cx")
    codex.provider_reported_model = "gpt-5.6-terra"
    codex.corsia_turn = TurnIdentity("cx-turn", 1)

    await cc_event_usage.record_call_llm_usage(
        "cc-thread", cc, message_id="m1", usage={"input_tokens": 3, "output_tokens": 1},
    )
    await codex_usage_calls.record_call_llm_usage(
        "cx-thread", codex, provider_turn_id="p1", ordinal=1, measured=dict(USAGE),
    )

    assert [c["reasoning_effort"] for c in captured] == ["medium", "high"]


def test_a_session_with_no_effort_is_unknown_not_a_default() -> None:
    assert usage_effort(SimpleNamespace(session=None)) is None
    assert usage_effort(SimpleNamespace(session=SimpleNamespace(effort=""))) is None
    assert usage_effort(SimpleNamespace(session=SimpleNamespace(effort=None))) is None


# -- limit history ------------------------------------------------------------


def _frame(percent: float, resets_at: str, *, observed_at: str) -> dict:
    return {
        "vendor": "claude-code", "kind": "five_hour", "used_percent": percent,
        "resets_at": resets_at, "source": "rate_limit_event", "observed_at": observed_at,
    }


def _record(provider: str, window: dict) -> None:
    harness_usage.record_usage(provider=provider, windows=[window], receipt=default_receipt(provider))


def _observations() -> list[tuple]:
    db = get_db()
    try:
        return [tuple(r) for r in db.execute(
            "SELECT account_id, window_key, window_kind, used_percent, resets_at, source, observed_at "
            "FROM limit_observations ORDER BY id")]
    finally:
        db.close()


@pytest.fixture()
def account(isolated_db):
    from connections.models import upsert_connection

    upsert_connection(type="cli", provider="claude-code", status="available", config={"binary": "/bin/test"})
    return default_receipt("claude-code")


def test_a_changed_window_appends_one_observation(account) -> None:
    _record("claude-code", _frame(10, "2026-10-09T00:00:00+00:00", observed_at="2026-10-08T20:00:00+00:00"))
    _record("claude-code", _frame(25, "2026-10-09T00:00:00+00:00", observed_at="2026-10-08T21:00:00+00:00"))
    rows = _observations()
    assert [(r[3], r[4], r[6]) for r in rows] == [
        (10, "2026-10-09T00:00:00+00:00", "2026-10-08T20:00:00+00:00"),
        (25, "2026-10-09T00:00:00+00:00", "2026-10-08T21:00:00+00:00"),
    ]
    assert {(r[0], r[1], r[2], r[5]) for r in rows} == {
        (account.account_id, "claude-code:five_hour:account", "five_hour", "rate_limit_event"),
    }


def test_a_new_reset_with_the_same_percent_is_a_new_observation(account) -> None:
    _record("claude-code", _frame(0, "2026-10-09T00:00:00+00:00", observed_at="2026-10-08T20:00:00+00:00"))
    _record("claude-code", _frame(0, "2026-10-09T05:00:00+00:00", observed_at="2026-10-09T00:30:00+00:00"))
    assert [r[4] for r in _observations()] == ["2026-10-09T00:00:00+00:00", "2026-10-09T05:00:00+00:00"]


def test_an_unchanged_window_appends_nothing(account) -> None:
    _record("claude-code", _frame(10, "2026-10-09T00:00:00+00:00", observed_at="2026-10-08T20:00:00+00:00"))
    _record("claude-code", _frame(10, "2026-10-09T00:00:00+00:00", observed_at="2026-10-08T20:30:00+00:00"))
    harness_usage.record_usage(provider="claude-code", facts={"plan": "max"}, receipt=account)
    assert len(_observations()) == 1


def test_the_history_index_serves_an_account_window_between_two_times(isolated_db) -> None:
    db = get_db()
    try:
        plan = [r[3] for r in db.execute(
            "EXPLAIN QUERY PLAN SELECT used_percent FROM limit_observations "
            "WHERE account_id=? AND window_key=? AND observed_at BETWEEN ? AND ?",
            ("a", "k", "t0", "t1"))]
    finally:
        db.close()
    assert plan == ["SEARCH limit_observations USING INDEX idx_limit_observations_window "
                    "(account_id=? AND window_key=? AND observed_at>? AND observed_at<?)"]


# -- migration ----------------------------------------------------------------


def test_the_step_is_registered_at_97() -> None:
    assert SCHEMA_VERSION == 156
    assert MIGRATIONS[97] is migrate_97_to_98


def test_the_migration_upgrades_an_existing_database_and_keeps_its_rows() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        db.execute("ALTER TABLE llm_usage DROP COLUMN reasoning_effort")
        db.execute("DROP TABLE limit_observations")
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,model,created_at) VALUES ('old','t','m','2026-10-01')")
        db.execute("PRAGMA user_version=97")
        db.commit()

        migrate_to_current(db, SCHEMA_VERSION)

        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("SELECT reasoning_effort FROM llm_usage WHERE id='old'").fetchone() == (None,)
        assert db.execute("SELECT COUNT(*) FROM limit_observations").fetchone() == (0,)
        index = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_limit_observations_window'"
        ).fetchone()
        assert index is not None


def test_the_migration_matches_a_fresh_install() -> None:
    def shape(db: sqlite3.Connection) -> tuple:
        columns = [tuple(r[1:3]) for r in db.execute("PRAGMA table_info(llm_usage)")]
        history = [tuple(r[1:3]) for r in db.execute("PRAGMA table_info(limit_observations)")]
        return columns, history

    with closing(sqlite3.connect(":memory:")) as fresh, closing(sqlite3.connect(":memory:")) as old:
        initialize_schema(fresh)
        initialize_schema(old)
        old.execute("ALTER TABLE llm_usage DROP COLUMN reasoning_effort")
        old.execute("DROP TABLE limit_observations")
        migrate_97_to_98(old)
        assert shape(old) == shape(fresh)


def test_a_reset_only_frame_on_a_read_window_appends_nothing(account) -> None:
    _record("claude-code", _frame(10, "2026-10-09T00:00:00+00:00", observed_at="2026-10-08T20:00:00+00:00"))
    reset_only = {
        "vendor": "claude-code", "kind": "five_hour", "resets_at": "2026-10-09T05:00:00+00:00",
        "source": "rate_limit_event", "observed_at": "2026-10-08T21:00:00+00:00",
    }
    _record("claude-code", reset_only)
    assert len(_observations()) == 1


def test_a_reading_without_a_time_or_kind_is_skipped_not_stored_as_text(account) -> None:
    undated = _frame(10, "2026-10-09T00:00:00+00:00", observed_at="2026-10-08T20:00:00+00:00")
    del undated["observed_at"]
    _record("claude-code", undated)
    kindless = _frame(11, "2026-10-09T00:00:00+00:00", observed_at="2026-10-08T20:00:00+00:00")
    del kindless["kind"]
    from connections.limit_history import append_changed
    db = get_db()
    try:
        assert append_changed(db, account.account_id, {}, {"w": kindless}) == 0
    finally:
        db.close()
    assert not any("None" in map(str, r) for r in _observations())


# -- the effort on the wire ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("level", "sent"), [("high", "high"), ("medium", None), ("auto", None)])
async def test_an_external_call_records_the_level_the_router_sent(monkeypatch, level, sent) -> None:
    from llm import base, router

    async def fake(*_a, **_k):
        return router.LLMResponse(content="ok")

    monkeypatch.setattr(base, "base_completion", fake)
    response = await router.chat_completion(
        [{"role": "user", "content": "hi"}], model="openai-compat/x", api_key="k",
        api_base="https://api.example.com/v1", reasoning_effort=level,
        reasoning_levels=["low", "high"],
    )
    assert response.effort_sent == sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adaptive", "requested", "sent"),
    [
        (True, "20000", "high"),      # a numeric budget maps to the level the wire carries
        (True, "HIGH", "high"),
        (True, "not-a-level", None),  # unparseable: no thinking is sent at all
        (True, "auto", None),
        (False, "medium", "medium"),  # a budget model carries the level that made its budget
        (False, "bogus", None),       # no budget, nothing sent
    ],
)
async def test_anthropic_records_what_its_request_carried(monkeypatch, adaptive, requested, sent) -> None:
    from llm import router
    from llm.providers import anthropic, anthropic_thinking

    seen = {}

    async def fake(_client, create_kwargs, **_k):
        seen.update(create_kwargs)
        return router.LLMResponse(content="ok")

    monkeypatch.setattr(anthropic, "_messages_with_retries", fake)
    monkeypatch.setattr(anthropic, "_get_client", lambda *_a, **_k: object())
    monkeypatch.setattr(anthropic, "_model_uses_adaptive_thinking", lambda _m: adaptive)
    monkeypatch.setattr(anthropic_thinking, "model_uses_adaptive_thinking", lambda _m: adaptive)
    response = await anthropic.anthropic_completion(
        [{"role": "user", "content": "hi"}], model="anthropic/x", max_tokens=100,
        reasoning_effort=requested,
    )
    assert response.effort_sent == sent
    assert ("thinking" in seen) == (sent is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("requested", "sent"), [("high", "high"), ("auto", None), ("none", None)])
async def test_openai_records_what_its_request_carried(monkeypatch, requested, sent) -> None:
    from llm import router
    from llm.providers import openai

    async def fake(_client, _kwargs, **_k):
        return router.LLMResponse(content="ok")

    monkeypatch.setattr(openai, "_responses_with_retries", fake)
    monkeypatch.setattr(openai, "_get_client", lambda *_a, **_k: object())
    response = await openai.openai_completion(
        [{"role": "user", "content": "hi"}], model="openai/gpt-5", reasoning_effort=requested,
    )
    assert response.effort_sent == sent
