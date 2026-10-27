"""Semantic coverage for Measurement's read-time Cost section."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from api.measurement_cost import build_cost
from api.measurement_prepared import DDL, PREPARED_OUTCOME_NODES
from core.measurement_scope import resolve_grain
from llm.api_equivalent import api_equivalent, is_long_context, price_identity
from llm.list_prices import ALIBABA_BEIJING, ALIBABA_SINGAPORE, LIST_PRICES, listing_for_host
from model_identity_fixtures import ALIBABA_HOST, seed_external_server

CC = "external_harness:claude-code"
CODEX = "external_harness:codex"
SONNET = next(price for price in LIST_PRICES if price.model == "claude-sonnet-5-5")
SOL = next(price for price in LIST_PRICES if price.model == "gpt-6.1-sol")
# Weeks in UTC, read on the Tuesday after the period the data sits in: the last completed period is
# 2026-09-21 for GRAIN and 2026-09-28 for NEXT_GRAIN.
GRAIN = resolve_grain("weeks", "UTC", now=datetime(2026, 9, 29, 12, tzinfo=timezone.utc))
NEXT_GRAIN = resolve_grain("weeks", "UTC", now=datetime(2026, 10, 6, 12, tzinfo=timezone.utc))
# These tests read one period of usage, so the usage bounds are narrowed to it.
PARAMS = {"scope_project": "all", "scope_agent": None, "user_id": "steve", "grain": GRAIN,
          "from_ts": "2026-09-21", "to_ts": "2026-09-28"}
NEXT_BOUNDS = {"grain": NEXT_GRAIN, "from_ts": "2026-09-28", "to_ts": "2026-10-05"}


def test_api_equivalent_is_pure_and_unknown_never_zero():
    assert SONNET.cache_write_per_million is not None
    assert api_equivalent("claude-code/claude-sonnet-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000) == (
        SONNET.input_per_million + SONNET.output_per_million + SONNET.cache_write_per_million + SONNET.cache_read_per_million)
    assert api_equivalent("claude-code/claude-sonnet-5-5[1m]", 1, 0, 0, 0) is not None
    for unknown in (
        ("claude-code/claude-sonnet-5-5", 1, 1, 1, None),   # an unreported bucket
        ("claude-code/claude-sonnet-5-5", 1, -1, 0, 0),
        ("claude-code/opus", 1, 0, 0, 0),                    # alias: identity not recorded
        ("claude-code/claude-unlisted", 1, 0, 0, 0),
        ("codex/gpt-5.3-codex-spark", 1, 0, 0, 0),           # no exact-identity rate
        ("external@namespace/qwen3.8-unlisted", 1, 0, 0, 0),  # id not on a provider's public list
        ("external@namespace/gpt-6.1-sol", 1, 0, 0, 0),       # a listed id of another provider is not Alibaba's
        ("external@namespace/qwen3.8-flash", 1, 0, 1, 0),     # cache write: no public rate
        ("external@namespace/qwen3.8-flash", 1, 0, 0, 1),     # cached input: no public rate
        ("omlx@local/qwen", 1, 0, 0, 0),
    ):
        assert api_equivalent(*unknown) is None, unknown
    assert price_identity("codex/gpt-6.1-sol") == "gpt-6.1-sol"
    assert price_identity("external@namespace/qwen3.8-flash", ALIBABA_SINGAPORE) == "qwen3.8-flash"
    assert price_identity("external@namespace/unlisted", ALIBABA_SINGAPORE) is None
    assert price_identity("omlx@local/qwen3.8-flash") is None


def test_an_external_identity_prices_only_on_its_own_clouds_regional_list():
    # The same open-weight id served by a self-hosted or another cloud server has no list rate.
    assert price_identity("external@self/qwen3.8-flash") is None
    assert price_identity("external@other/qwen3.8-flash", "elsewhere") is None
    assert api_equivalent("external@self/deepseek-v4.1-flash", 1, 0, 0, 0) is None
    assert listing_for_host(ALIBABA_HOST) == ALIBABA_SINGAPORE
    assert listing_for_host("WS-1.cn-beijing.maas.aliyuncs.com.") == ALIBABA_BEIJING
    # A region whose list this table does not carry, or a host naming no region, has no rate.
    for host in ("127.0.0.1", "aliyuncs.com", "maas.aliyuncs.com", "ap-southeast-1.maas.aliyuncs.com",
                 "maas.aliyuncs.com.example.com", "notmaas.aliyuncs.com", "ws.us-east-1.maas.aliyuncs.com",
                 "dashscope-intl.aliyuncs.com"):
        assert listing_for_host(host) is None, host


def test_each_identity_is_listed_once_per_list_and_shares_one_idle_window():
    # A second row for the same identity on the same list would silently replace the first.
    keys = [(price.model, price.listing) for price in LIST_PRICES]
    assert len(keys) == len(set(keys))
    windows: dict[str, set] = {}
    for price in LIST_PRICES:
        if price.idle_utc_hours is not None:
            windows.setdefault(price.model, set()).add(price.idle_utc_hours)
    assert all(len(found) == 1 for found in windows.values()), windows


def test_alibaba_prices_each_region_at_its_own_list_by_call_hours_and_unpublished_rates_unknown():
    deepseek, sg, bj = "external@ns/deepseek-v4.1-flash", ALIBABA_SINGAPORE, ALIBABA_BEIJING
    assert api_equivalent(deepseek, 1_000_000, 1_000_000, 0, 1_000_000, listing=sg) == pytest.approx(0.3 + 1.2 + 0.03)
    assert api_equivalent(deepseek, 1_000_000, 1_000_000, 0, 1_000_000, idle_hours=True, listing=sg) == pytest.approx(
        0.15 + 0.6 + 0.015)
    assert api_equivalent(deepseek, 1_000_000, 1_000_000, 0, 1_000_000, listing=bj) == pytest.approx(
        0.283 + 1.131 + 0.0283)
    assert api_equivalent(deepseek, 1_000_000, 1_000_000, 0, 1_000_000, idle_hours=True, listing=bj) == pytest.approx(
        0.141 + 0.565 + 0.0141)
    assert api_equivalent("external@ns/qwen3.8-flash", 1_000_000, 1_000_000, 0, 0, listing=sg) == pytest.approx(0.15 + 0.47)
    assert api_equivalent("external@ns/qwen3.8-flash", 1_000_000, 1_000_000, 0, 0, listing=bj) == pytest.approx(
        0.113 + 0.382)
    assert api_equivalent("external@ns/glm-5.3", 1_000_000, 1_000_000, 0, 0, idle_hours=True,
                          listing=sg) == pytest.approx(1.4 + 4.4)
    assert api_equivalent("external@ns/glm-5.3", 1, 0, 0, 1, listing=sg) is None


def test_api_equivalent_uses_the_public_per_call_input_tier_and_never_frees_an_absent_price():
    assert SOL.long_input_per_million is not None
    assert is_long_context(272_000, 0, 0) is False
    assert is_long_context(272_001, 0, 0) is True
    assert is_long_context(272_000, 1, 0) is True
    assert is_long_context(272_000, 0, 1) is True
    assert api_equivalent("codex/gpt-6.1-sol", 272_000, 1_000_000, 0, 0) == pytest.approx(
        (272_000 * SOL.input_per_million + 1_000_000 * SOL.output_per_million) / 1_000_000)
    assert api_equivalent("codex/gpt-6.1-sol", 272_001, 0, 0, 0, long_context=True) == pytest.approx(
        272_001 * SOL.long_input_per_million / 1_000_000)
    assert api_equivalent("codex/gpt-5.4", 1, 0, 0, 0) is not None
    assert api_equivalent("codex/gpt-5.4", 1, 0, 1, 0) is None
    assert api_equivalent("codex/gpt-5.3-codex", 1, 0, 1, 0) is None


def _seed(db, rows):
    db.execute("CREATE TEMP TABLE measurement_scope_threads (id TEXT PRIMARY KEY, root_id TEXT)")
    db.execute("CREATE TEMP TABLE measurement_outcome_periods (period TEXT PRIMARY KEY, outcomes INTEGER NOT NULL, first_try INTEGER NOT NULL, rejections INTEGER NOT NULL)")
    # The fixture database seeds placeholder accounts with an unreadable created_at; a payer like that
    # now reads as unrecorded, so a test about usage starts with no accounts.
    db.execute("DELETE FROM vendor_accounts")
    db.execute(DDL[PREPARED_OUTCOME_NODES])  # the settled outcomes: none here, only the periods' counts below
    db.execute("INSERT INTO measurement_scope_threads VALUES ('thread', 'thread')")
    db.execute("INSERT INTO measurement_outcome_periods VALUES ('2026-09-21', 2, 2, 0)")
    for index, row in enumerate(rows):
        model, source, p, c, cw, cr, total, cost, *ids = row
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,turn_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,"
            "cache_read_tokens,total_tokens,cost_usd,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ids[0] if ids else f"row{index}", "thread", ids[1] if len(ids) > 1 else None, model,
             p, c, cw, cr, total, cost, source, "2026-09-22T12:00:00Z"),
        )
    # A different thread's usage is outside every scope below.
    db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,source,created_at) VALUES ('other','elsewhere','claude-code/claude-sonnet-5-5',1,?,'2026-09-22T12:00:00Z')", (CC,))


def test_scope_is_complete_from_its_own_direct_charges_whatever_else_the_workspace_used(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        _seed(db, [("openai/gpt-6.1", "conversation", 1, 0, 0, 0, 1, 2.5)])
        # Unlinked and unpriced subscription usage in an unrelated thread.
        db.execute("INSERT INTO llm_usage (id,thread_id,model,source,created_at) VALUES ('unlinked','elsewhere','claude-code/claude-sonnet-5-5',?,'2026-09-22T12:00:00Z')", (CC,))
        db.execute("INSERT INTO llm_usage (id,thread_id,model,source,created_at,vendor_account_id) VALUES ('unpriced','elsewhere','claude-code/claude-sonnet-5-5',?,'2026-09-22T12:00:00Z','missing-account')", (CC,))
        known = build_cost(db, PARAMS)["actual"]
        db.execute("INSERT INTO llm_usage (id,thread_id,model,source,created_at,cost_usd) VALUES ('nocharge','thread','openai/gpt-6.1','conversation','2026-09-22T12:00:00Z',NULL)")
        unknown = build_cost(db, PARAMS)["actual"]
    finally:
        db.close()

    assert known["direct_api_dollars"] == 2.5 and known["unknown_charge_records"] == 0
    assert known["complete"] is True
    assert unknown["unknown_charge_records"] == 1 and unknown["complete"] is False
    # Unlinked usage is not a coverage input: nothing the user can add would change it.
    assert "unlinked_subscription_records" not in known["workspace_subscription"]
    assert known["workspace_subscription"]["subscription_coverage"] == "none"


def test_api_equivalent_prices_only_the_proven_short_context_bound_before_weekly_aggregation(isolated_db):
    from infra.database import get_db

    model = "codex/gpt-6.1-sol"
    turn_id = "terminal-turn"
    terminal_id = hashlib.sha256(f"turn-usage\0{turn_id}\0terminal".encode()).hexdigest()[:24]
    db = get_db()
    try:
        _seed(db, [
            # Exactly 272K input remains short even with a large output.
            (model, CODEX, 272_000, 1_000_000, 0, 0, 1_272_000, None, "000000000000000000000001"),
            # This exact terminal receipt is an opaque aggregate above the short bound.
            (model, CODEX, 272_001, 0, 0, 0, 272_001, None, terminal_id, turn_id),
            # Together these exceed 272K, but each original row proves short.
            (model, CODEX, 200_000, 0, 0, 0, 200_000, None, "000000000000000000000003"),
            (model, CODEX, 200_000, 0, 0, 0, 200_000, None, "000000000000000000000004"),
            # Cache-write price is absent for gpt-5.4, so positive usage is Unknown.
            ("codex/gpt-5.4", CODEX, 1, 0, 1, 0, 2, None, "000000000000000000000005"),
        ])
        result = build_cost(db, PARAMS)
    finally:
        db.close()
    period = result["latest"]
    assert result["definition_version"] == 1
    assert period["dollars"] == pytest.approx(10.544 + 0.8)
    assert (period["priced_records"], period["usage_records"]) == (3, 5)
    assert result["coverage"] == {"priced_records": 3, "usage_records": 5}
    assert {item["kind"]: item["records"] for item in result["excluded"]} == {
        "context_tier_unproven": 1, "cache_rate_unpublished": 1}


def test_admission_prices_only_rows_with_proven_buckets_and_contract(isolated_db):
    from infra.database import get_db

    sonnet = "claude-code/claude-sonnet-5-5"
    db = get_db()
    try:
        _seed(db, [
            (sonnet, CC, 1_000_000, 0, 0, 1_000_000, 2_000_000, None),   # priced: cache needs the Claude Code source
            (sonnet, CC, 1_000_000, 0, 0, 0, None, None),                 # priced: total NULL is not evidence against
            (sonnet, "conversation", 10, 0, 0, 5, 15, None),               # cache without a proven contract
            (sonnet, "conversation", 1_000_000, 0, 0, 0, 1_000_000, None),  # zero cache: both contracts agree
            ("codex/gpt-6.1-sol", CODEX, 10, 0, 0, 0, 10, None),            # Codex may be thread-cumulative
            ("codex/gpt-6.1-sol", "conversation", 10, 0, 0, 0, 10, None),   # a Codex model is never priced
            (sonnet, CC, 10, 0, 0, None, None, None),                       # unreported bucket
            (sonnet, CC, 10, 0, 0, 0, 99, None),                            # total disagrees
            ("claude-code/opus", CC, 10, 0, 0, 0, 10, None),                # alias identity unrecorded
            ("external@ns/unlisted-model", "conversation", 10, 0, 0, 0, 10, 0.0),
            ("omlx@local/qwen", "conversation", 10, 0, 0, 0, 10, 0.0),
        ])
        result = build_cost(db, PARAMS)
    finally:
        db.close()
    period = result["latest"]
    assert period["dollars"] == pytest.approx(SONNET.input_per_million * 3 + SONNET.cache_read_per_million)
    assert (period["priced_records"], period["usage_records"]) == (3, 11)
    assert result["coverage"] == {"priced_records": 3, "usage_records": 11}
    assert result["not_api_priced_records"] == 2
    assert {item["kind"]: item["records"] for item in result["excluded"]} == {
        "contract_unproven": 3, "tokens_unreported": 1, "tokens_inconsistent": 1, "no_list_price": 1}
    no_list_price = next(item for item in result["excluded"] if item["kind"] == "no_list_price")
    assert no_list_price["models"] == [{"label": "opus (version not recorded)", "records": 1}]
    assert period["per_outcome"] == pytest.approx(period["dollars"] / 2)


@pytest.mark.parametrize(
    ("model", "source", "cache_read", "expected"),
    [
        ("claude-code/claude-sonnet-5-5", CC, 5, "ok"),
        ("claude-code/claude-sonnet-5-5", None, 5, "contract_unproven"),    # NULL source is never proof
        ("claude-code/claude-sonnet-5-5", None, 0, "ok"),                   # zero cache: contracts agree
        (None, CC, 5, "contract_unproven"),                                 # NULL model cannot match the harness
        ("codex/gpt-6.1-sol", CC, 5, "contract_unproven"),                  # contradictory source and model
        ("codex/gpt-6.1-sol", CC, 0, "contract_unproven"),
        ("codex/gpt-6.1-sol", None, 0, "contract_unproven"),
        ("claude-code/claude-sonnet-5-5", CODEX, 0, "contract_unproven"),
        ("claude-code/claude-sonnet-5-5", "conversation", 5, "contract_unproven"),
    ],
)
def test_admission_sql_is_total_over_null_source_and_model(model, source, cache_read, expected):
    """A NULL must fall to a named exclusion, never through ``NOT NULL`` into 'ok'."""
    assert _admission(model, source, cache_read) == expected


@pytest.mark.parametrize(
    ("source", "created_at", "expected"),
    [
        ("conversation", "2026-09-17T00:00:00Z", "ok"),
        ("conversation", "2026-09-16T23:59:59Z", "contract_unproven"),
        (None, "2026-09-22T12:00:00Z", "contract_unproven"),
        (CC, "2026-09-22T12:00:00Z", "contract_unproven"),
    ],
)
def test_an_external_conversation_row_with_cache_is_admitted_from_the_disjoint_contract(source, created_at, expected):
    assert _admission("external@ns/qwen3.8-flash", source, 5, created_at=created_at) == expected
    assert _admission("omlx@local/qwen", source, 5, created_at=created_at) == "contract_unproven"


def test_alibaba_usage_prices_by_hour_and_names_unpublished_cache_rates(isolated_db):
    from infra.database import get_db

    deepseek, qwen = "external@ns/deepseek-v4.1-flash", "external@ns/qwen3.8-flash"
    db = get_db()
    try:
        seed_external_server(db, "ns", ALIBABA_HOST)
        seed_external_server(db, "self", "192.168.1.5", "lan")
        seed_external_server(db, "other", "models.example.com")
        _seed(db, [
            (deepseek, "conversation", 1_000_000, 0, 0, 1_000_000, 2_000_000, None, "busy"),
            (deepseek, "conversation", 1_000_000, 0, 0, 0, 1_000_000, None, "idle"),
            (qwen, "conversation", 1_000_000, 0, 0, 0, 1_000_000, None, "zero-cache"),
            (qwen, "conversation", 10, 0, 0, 5, 15, None, "cached"),
            ("external@ns/unlisted-model", "conversation", 10, 0, 0, 0, 10, 0.0, "unlisted"),
            ("external@self/deepseek-v4.1-flash", "conversation", 10, 0, 0, 0, 10, 0.0, "self-hosted"),
            ("external@other/qwen3.8-flash", "conversation", 10, 0, 0, 0, 10, 0.0, "other-cloud"),
            ("omlx@local/qwen", "conversation", 10, 0, 0, 0, 10, 0.0, "local"),
        ])
        db.execute("UPDATE llm_usage SET created_at='2026-09-22T14:00:00Z' WHERE id IN ('idle','self-hosted')")
        result = build_cost(db, PARAMS)
    finally:
        db.close()
    period = result["latest"]
    assert period["dollars"] == pytest.approx(0.3 + 0.03 + 0.15 + 0.15)
    assert (result["coverage"], result["not_api_priced_records"]) == (
        {"priced_records": 3, "usage_records": 8}, 4)
    assert {item["kind"]: item["records"] for item in result["excluded"]} == {
        "cache_rate_unpublished": 1}


def test_coverage_counts_the_headline_window_not_the_drawn_range(isolated_db):
    from infra.database import get_db

    sonnet = "claude-code/claude-sonnet-5-5"
    db = get_db()
    try:
        _seed(db, [(sonnet, CC, 1_000, 0, 0, 0, 1_000, None)])
        # Weeks before the headline window: drawn on the chart, not part of the window's figures.
        for index, day in enumerate(("2026-08-11T12:00:00Z", "2026-08-12T12:00:00Z")):
            db.execute(
                "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,"
                "cache_read_tokens,total_tokens,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"early{index}", "thread", sonnet, 1_000, 0, 0, 0, 1_000, CC, day))
        db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,source,created_at) VALUES "
                   "('early-local','thread','omlx@local/qwen',5,'conversation','2026-08-11T12:00:00Z')")
        result = build_cost(db, {**PARAMS, "from_ts": "2026-08-01"})
    finally:
        db.close()
    assert GRAIN.latest.first == "2026-08-31" and "2026-08-10" in GRAIN.served
    assert result["coverage"] == {"priced_records": 1, "usage_records": 1}
    assert result["not_api_priced_records"] == 0
    assert result["excluded"] == []


def test_a_local_record_stays_in_the_denominator_and_is_named_not_api_priced(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        _seed(db, [("claude-code/claude-sonnet-5-5", CC, 1_000, 0, 0, 0, 1_000, None),
                   ("omlx@local/qwen", "conversation", 10, 0, 0, 0, 10, 0.0)])
        result = build_cost(db, PARAMS)
    finally:
        db.close()
    assert result["coverage"] == {"priced_records": 1, "usage_records": 2}
    assert result["latest"]["usage_records"] == 2
    assert result["not_api_priced_records"] == 1


def test_a_record_is_classified_by_its_resolved_model_identity_not_its_raw_selector(isolated_db):
    from infra.database import get_db
    from model_identity_fixtures import seed_manifest

    db = get_db()
    try:
        _seed(db, [("claude-code/opus", CC, 1_000_000, 0, 0, 0, 1_000_000, None, "alias-row")])
        seed_manifest(db, "m1", "claude-code", "claude-sonnet-5-5", "catalog")
        db.execute("UPDATE llm_usage SET context_manifest_id='m1' WHERE id='alias-row'")
        result = build_cost(db, PARAMS)
    finally:
        db.close()
    # The alias selector alone has no list price; the identity its turn resolved to does.
    assert result["coverage"] == {"priced_records": 1, "usage_records": 1}
    assert result["latest"]["dollars"] == pytest.approx(SONNET.input_per_million)
    assert result["excluded"] == []


def test_checkpoint_and_turn_resolve_and_price_the_same_concrete_claude_identity(isolated_db):
    from core.cost_identity import IdentityResolver
    from infra.database import get_db

    db = get_db()
    try:
        resolve = IdentityResolver(db)
        checkpoint = resolve({"model": "claude-code/claude-sonnet-5-5"})
        turn = resolve({
            "model": "claude-code/sonnet", "manifest_harness": "claude-code",
            "manifest_model": "claude-sonnet-5-5", "manifest_source": "catalog",
        })
    finally:
        db.close()
    assert checkpoint.key == turn.key == "claude-code/claude-sonnet-5-5"
    assert price_identity(checkpoint.key) == "claude-sonnet-5-5"


RECEIPT_ID = "0123456789abcdef01234567"  # the 24-hex shape of turn-usage and checkpoint receipts


@pytest.mark.parametrize(
    ("usage_id", "model", "source", "cache_read", "expected"),
    [
        (RECEIPT_ID, "codex/gpt-6.1-sol", CODEX, 5, "ok"),                     # post-contract turn writer, proven by id shape
        (RECEIPT_ID, "codex/gpt-6.1-sol", CODEX, 0, "ok"),
        (RECEIPT_ID, "codex/gpt-6.1-sol", "checkpoint", 5, "ok"),             # post-contract checkpoint writer
        (RECEIPT_ID, "claude-code/claude-sonnet-5-5", "checkpoint", 5, "ok"),
        (RECEIPT_ID, "claude-code/claude-sonnet-5-5", "host_access_action_review", 5, "ok"),
        (RECEIPT_ID, "codex/gpt-6.1-sol", "host_access_application_review", 5, "ok"),
        ("direct-id", "external@provider/model", "checkpoint", 0, "ok"),  # zero-cache native checkpoint remains admitted
        ("direct-id", "claude-sonnet-4-5", "host_access_action_review", 0, "ok"),  # bare Claude identity has no receipt contract
        ("8f3a9c1e-b2d", "codex/gpt-6.1-sol", CODEX, 0, "contract_unproven"),  # old direct writer: format proves nothing
        ("8f3a9c1e-b2d", "gpt-5", CODEX, 0, "contract_unproven"),  # a Codex-source row stays unproven whatever its model
        ("8f3a9c1e-b2d", "claude-code/claude-sonnet-5-5", "host_access_action_review", 5, "ok"),  # reviewer copied disjoint buckets since its first row
        ("8f3a9c1e-b2d", "codex/gpt-6.1-sol", "host_access_application_review", 5, "ok"),  # a fully reported Codex reviewer row is from_codex
        ("8f3a9c1e-b2d", "claude-code/claude-sonnet-5-5", "conversation", 5, "contract_unproven"),
        ("0123456789ABCDEF01234567", "codex/gpt-6.1-sol", CODEX, 0, "contract_unproven"),  # not a lowercase hex digest
        ("0123456789ABCDEF01234567", "codex/gpt-6.1-sol", "checkpoint", 0, "contract_unproven"),
        ("0123456789abcdef0123456", "codex/gpt-6.1-sol", CODEX, 0, "contract_unproven"),   # wrong length
        (None, "codex/gpt-6.1-sol", CODEX, 0, "contract_unproven"),
        (RECEIPT_ID, "codex/gpt-6.1-sol", CC, 0, "contract_unproven"),         # contradictory source: still Unknown
        (RECEIPT_ID, "codex/gpt-6.1-sol", "conversation", 5, "contract_unproven"),
        (RECEIPT_ID, "codex/gpt-6.1-sol", None, 0, "contract_unproven"),
    ],
)
def test_codex_is_priced_only_on_a_receipt_shaped_id(usage_id, model, source, cache_read, expected):
    assert _admission(model, source, cache_read, usage_id) == expected


def _admission(model, source, cache_read, usage_id="direct-id", created_at="2026-09-22T12:00:00Z"):
    import sqlite3

    from core.measurement_pricing import ADMISSION_SQL

    db = sqlite3.connect(":memory:")  # nullable columns: the ledger's NOT NULL must not be the only guard
    db.execute("CREATE TABLE llm_usage (id TEXT, model TEXT, source TEXT, prompt_tokens, completion_tokens, "
               "cache_creation_tokens, cache_read_tokens, total_tokens, created_at TEXT)")
    db.execute("INSERT INTO llm_usage VALUES (?,?,?,10,0,0,?,?,?)",
               (usage_id, model, source, cache_read, 10 + cache_read, created_at))
    return db.execute(f"SELECT {ADMISSION_SQL} FROM llm_usage u").fetchone()[0]


def test_a_period_is_unknown_only_when_nothing_in_it_is_priceable(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        _seed(db, [("claude-code/opus", CC, 10, 0, 0, 0, 10, None)])
        assert build_cost(db, PARAMS)["series"] == []
    finally:
        db.close()


def test_actual_is_scoped_ledger_policy_not_signed_in_accounts(isolated_db):
    from infra.database import get_db

    sonnet = "claude-code/claude-sonnet-5-5"
    db = get_db()
    try:
        _seed(db, [
            ("some-native/model", "conversation", 1, 0, 0, 0, 1, 2.5),
            ("omlx@local/qwen", "conversation", 1, 0, 0, 0, 1, 0.0),
        ])
        complete = build_cost(db, PARAMS)["actual"]
        assert {key: complete[key] for key in ("subscription_records", "unknown_charge_records", "direct_api_dollars", "complete")} == {"subscription_records": 0, "unknown_charge_records": 0, "direct_api_dollars": 2.5, "complete": True}
        assert complete["workspace_subscription"] == {"subscription_dollars": None, "overage_dollars": None, "subscription_facts": 0, "overage_facts": 0, "subscription_unrecorded_accounts": 0, "subscription_coverage": "none", "overage_coverage": "none", "overlapping_overage": [], "overage_unrecorded": False, "allocation": "unavailable"}
        # Subscription usage does not make direct charges incomplete even with a legacy
        # cost and no signed-in account; a native call without a charge is unknown.
        db.execute("INSERT INTO llm_usage (id,thread_id,model,source,created_at,cost_usd) VALUES ('sub','thread',?,?,'2026-09-22T12:00:00Z',9.0)", (sonnet, CC))
        db.execute("INSERT INTO llm_usage (id,thread_id,model,source,created_at,cost_usd) VALUES ('nocharge','thread','some-native/model','conversation','2026-09-22T12:00:00Z',NULL)")
        actual = build_cost(db, PARAMS)["actual"]
    finally:
        db.close()
    assert {key: actual[key] for key in ("subscription_records", "unknown_charge_records", "direct_api_dollars", "complete")} == {"subscription_records": 1, "unknown_charge_records": 1, "direct_api_dollars": 2.5, "complete": False}


@pytest.mark.parametrize(
    ("model", "source", "cost", "expected"),
    [
        ("claude-code/opus", "conversation", None, "subscription"),       # canonical identity alone is evidence
        ("codex/gpt-6.1-sol", "conversation", 3.0, "subscription"),       # a legacy cost is not a charge
        ("some-transport/alias", CC, None, "subscription"),               # persisted harness source, alias model
        ("some-transport/alias", CODEX, 1.0, "subscription"),
        ("openai/gpt-6.1", "conversation", 4.0, "known"),                 # native API call: charged, known
        ("openai/gpt-6.1", "conversation", None, "unknown"),              # native call without a charge
        ("omlx@local/qwen", CC, None, "none"),                            # local wins: no charge, not subscription
    ],
)
def test_actual_classifies_by_recorded_model_and_source_never_the_current_catalog(isolated_db, model, source, cost, expected):
    from infra.database import get_db

    db = get_db()
    try:
        _seed(db, [(model, source, 1, 0, 0, 0, 1, cost)])
        actual = build_cost(db, PARAMS)["actual"]
    finally:
        db.close()
    assert {key: actual[key] for key in ("subscription_records", "unknown_charge_records", "direct_api_dollars", "complete")} == {
        "subscription": {"subscription_records": 1, "unknown_charge_records": 0, "direct_api_dollars": 0.0, "complete": True},
        "known": {"subscription_records": 0, "unknown_charge_records": 0, "direct_api_dollars": 4.0, "complete": True},
        "unknown": {"subscription_records": 0, "unknown_charge_records": 1, "direct_api_dollars": 0.0, "complete": False},
        "none": {"subscription_records": 0, "unknown_charge_records": 0, "direct_api_dollars": 0.0, "complete": True},
    }[expected]


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import measurement

    app = FastAPI()
    app.include_router(measurement.router)
    app.dependency_overrides[measurement.require_user] = lambda: {"id": "steve"}
    return TestClient(app)


def test_cost_route_requires_the_whole_scope_and_serves_scoped_period_outcomes(isolated_db, measurement_clock):
    from infra.database import get_db
    from tests.test_measurement_api import _seed_outcomes, _seed_thread

    db = get_db()
    try:
        _seed_thread(db, "root", agent=True)
        _seed_outcomes(db, "root", count=4)
        db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at) VALUES ('u','root','claude-code/claude-sonnet-5-5',1000000,0,0,0,1000000,?,'2026-09-22T12:00:00Z')", (CC,))
        db.commit()
    finally:
        db.close()
    client = _client()
    assert client.get("/api/measurement/cost?grain=weeks&timezone=UTC&project=all").status_code == 422
    assert client.get("/api/measurement/cost?grain=weeks&timezone=UTC&agent=none").status_code == 422
    response = client.get("/api/measurement/cost?grain=weeks&timezone=UTC&project=all&agent=none")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == {"project": "all", "agent": None}
    assert (body["latest"]["from"], body["latest"]["to"]) == ("2026-08-31", "2026-09-27"), "the window's last day, as /series names it"
    assert (body["previous"]["from"], body["previous"]["to"]) == ("2026-08-03", "2026-08-30")
    assert body["latest"]["outcomes"] == 4
    assert body["latest"]["dollars"] == pytest.approx(SONNET.input_per_million)
    assert body["latest"]["per_outcome"] == pytest.approx(SONNET.input_per_million / 4)
    assert body["actual"]["subscription_records"] == 1 and body["actual"]["complete"] is True


def test_external_endpoint_is_not_api_priced_without_a_list_price_but_is_not_local() -> None:
    from llm.api_equivalent import is_not_api_priced
    from llm.pricing import is_local_model

    assert is_not_api_priced("external@ns/unlisted-model", None) is True
    assert is_not_api_priced("omlx@ns/anything", None) is True
    assert is_not_api_priced("external@ns/qwen3.8-flash", ALIBABA_SINGAPORE) is False
    assert is_local_model("external@ns/unlisted-model") is False


def test_spending_reads_external_as_unknown_or_priced_never_local() -> None:
    from api.costs import _stored_cost_class

    assert _stored_cost_class({"model": "external@ns/m", "cost_usd": None}) == "no_pricing"
    assert _stored_cost_class({"model": "external@ns/m", "cost_usd": 0.0}) == "no_pricing"  # legacy placeholder zero
    assert _stored_cost_class({"model": "external@ns/m", "cost_usd": 0.2}) == "priced"
    assert _stored_cost_class({"model": "claude-code/claude-opus-4-8", "cost_usd": 0.0}) == "priced"
    assert _stored_cost_class({"model": "omlx@ns/m", "cost_usd": 0.0}) == "local"


def test_workspace_bill_uses_the_one_measurement_window_rule(isolated_db):
    from connections import subscription_billing as sb
    from infra.database import get_db

    from test_subscription_billing import _set, _solo

    # from_ts/to_ts are the bounds query_params supplies; the context reads them, it does not derive a second window.
    bounded = {**PARAMS, "from_ts": "2026-09-21", "to_ts": "2026-09-25"}
    db = get_db()
    try:
        _seed(db, [])
        account = _solo(db)
        _set(db, account["id"], 3100, 22)
        sb.record_overage(account["id"], amount_cents=0, month="2026-09", expected_billing_revision=1, db=db)
        workspace = build_cost(db, bounded)["actual"]["workspace_subscription"]
    finally:
        db.close()

    assert workspace["subscription_coverage"] == "complete" and workspace["subscription_dollars"] == 31.0
    assert workspace["overage_coverage"] == "complete"


def _seed_priced_period(count: int, size_each: str | None) -> None:
    from infra.database import get_db
    from tests.test_measurement_api import _seed_outcomes, _seed_thread

    db = get_db()
    try:
        _seed_thread(db, "root", agent=True)
        _seed_outcomes(db, "root", count=count)
        if size_each:
            db.execute("UPDATE plan_nodes SET size=?", (size_each,))
        db.execute("INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,total_tokens,source,created_at) VALUES ('u','root','claude-code/claude-sonnet-5-5',1000000,0,0,0,1000000,?,'2026-09-22T12:00:00Z')", (CC,))
        db.commit()
    finally:
        db.close()


def test_cost_per_hour_is_all_priced_spend_over_the_hours_of_size_rated_accepted_work(isolated_db, measurement_clock):
    _seed_priced_period(24, "M")
    period = _client().get("/api/measurement/cost?grain=weeks&timezone=UTC&project=all&agent=none").json()["latest"]
    # 24 outcomes of class M (2 h each) are 48 hours; every priced call in the period is the numerator.
    assert (period["sized"], period["hours"]) == (24, pytest.approx(48))
    assert period["per_hour"] == pytest.approx(SONNET.input_per_million / 48)


def test_cost_per_hour_is_withheld_never_zero_under_the_minimum_n_or_without_ratings(isolated_db, measurement_clock):
    _seed_priced_period(19, "M")
    period = _client().get("/api/measurement/cost?grain=weeks&timezone=UTC&project=all&agent=none").json()["latest"]
    assert (period["sized"], period["per_hour"]) == (19, None)
    assert period["dollars"] == pytest.approx(SONNET.input_per_million)


def test_cost_per_hour_without_any_size_is_unknown(isolated_db, measurement_clock):
    _seed_priced_period(24, None)
    period = _client().get("/api/measurement/cost?grain=weeks&timezone=UTC&project=all&agent=none").json()["latest"]
    assert (period["sized"], period["hours"], period["per_hour"]) == (0, 0, None)


@pytest.mark.parametrize("grain, before, first", [("weeks", "2026-08-30", "2026-08-31"), ("days", "2026-08-30", "2026-08-31")])
def test_the_cost_series_starts_with_the_size_rating_instrument_not_with_the_first_spend(isolated_db, monkeypatch, grain, before, first):
    from infra.database import get_db
    from tests.plan_writer_authorizer import raw_plan_seed
    from tests.test_measurement_api import _seed_thread

    # Tuesday 2026-09-08: both grains' spines reach back over the size series' start (2026-08-31).
    monkeypatch.setattr("core.measurement_scope._now", lambda: datetime(2026, 9, 8, 12, tzinfo=timezone.utc))
    db = get_db()
    try:
        _seed_thread(db, "root", agent=True)
        db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES ('cost-plan','thread','root','active',1,'t','t')")
        with raw_plan_seed(db):
            db.execute(
                "INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,"
                "requirements_revision,revision,size,created_at,updated_at) "
                "VALUES ('cost-size','cost-plan',0,'task','t','t','accepted','cost-size',1,1,'S',?,?)",
                (f"{first}T12:00:00Z", f"{first}T12:00:00Z"),
            )
            db.execute(
                "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
                "VALUES ('cost-size-event','cost-size','test','accepted','active','accepted',?)",
                (f"{first}T12:00:00Z",),
            )
        for day in (before, first, "2026-09-02"):
            db.execute(
                "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,cache_read_tokens,"
                "total_tokens,source,created_at) VALUES (?,'root','claude-code/claude-sonnet-5-5',1000000,0,0,0,1000000,?,?)",
                (f"u-{day}", CC, f"{day}T12:00:00Z"))
        db.commit()
    finally:
        db.close()
    body = _client().get(f"/api/measurement/cost?grain={grain}&timezone=UTC&project=all&agent=none").json()
    served = [row["period"] for row in body["series"]]
    # Spend exists before the instrument, but no period before it is served: it could only read as no data.
    assert served[0] == first and before not in served
    assert all(row["dollars"] is not None for row in body["series"])
