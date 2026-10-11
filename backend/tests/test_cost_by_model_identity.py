"""Spend's By model names each row by the identity Models uses, keeping unknown cost unknown."""

from datetime import timedelta

import pytest

from model_identity_fixtures import LOCAL_ID, LOCAL_MODEL, seed_identity_catalog, seed_manifest


@pytest.fixture
def by_model(isolated_db, monkeypatch):
    from api import costs
    from infra.database import get_db
    from infra.time import utc_now

    monkeypatch.setattr(costs, "authenticate", lambda _: {"id": "steve"})
    clock = utc_now().date()
    # The Wednesday of the last completed week: inside 12w, never in the partial week.
    at = f"{(clock - timedelta(days=clock.weekday() + 5)).isoformat()}T12:00:00Z"
    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,title,agent_enabled,agent_status,created_at,updated_at) VALUES ('root','steve','root',0,'active','t','t')")
    seed_identity_catalog(db)
    seed_manifest(db, "opus-48", "claude-code", "claude-opus-4-8", "catalog")

    def usage(usage_id: str, model: str, cost: float | None, manifest: str | None = None) -> None:
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,total_tokens,cost_usd,source,created_at,context_manifest_id) "
            "VALUES (?, 'root', ?, 10, 5, 15, ?, 'turn', ?, ?)",
            (usage_id, model, cost, at, manifest),
        )

    async def rows(*seeded: tuple) -> dict[str, dict]:
        for index, row in enumerate(seeded):
            usage(f"usage-{index}", *row)
        db.commit()
        summary = await costs.cost_summary("test-token", project="all", agent="none", range="12w")
        return {row["model"]: row for row in summary["by_model"]}

    try:
        yield rows
    finally:
        db.close()


@pytest.mark.asyncio
async def test_alias_nothing_resolves_stays_its_own_row(by_model):
    rows = await by_model(("claude-code/opus", 1.0), ("claude-code/claude-opus-4-8", 2.0))
    assert set(rows) == {"claude-code/opus", "claude-code/claude-opus-4-8"}
    # Named by its selector, never by the catalog's versioned name for it.
    assert rows["claude-code/opus"]["label"] == "opus (version not recorded)"
    assert rows["claude-code/claude-opus-4-8"]["label"] == "Opus 4.8"  # the catalog's projected name


@pytest.mark.asyncio
async def test_priced_and_unpriced_calls_of_one_identity_merge_without_zeroing(by_model):
    rows = await by_model(("claude-code/claude-opus-4-8", 2.0), ("claude-code/opus", None, "opus-48"))
    (row,) = rows.values()
    assert (row["model"], row["cost_usd"], row["unknown_cost_rows"], row["api_calls"]) == (
        "claude-code/claude-opus-4-8", 2.0, 1, 2)


@pytest.mark.asyncio
async def test_unpriced_identity_stays_unknown_never_zero(by_model):
    rows = await by_model(("claude-code/opus", None, "opus-48"))
    (row,) = rows.values()
    assert (row["cost_usd"], row["cost_class"], row["unknown_cost_rows"]) == (None, "no_pricing", 1)


@pytest.mark.asyncio
async def test_local_model_is_named_by_the_catalog_and_classed_local(by_model):
    rows = await by_model((LOCAL_ID, None))
    (row,) = rows.values()
    assert (row["model"], row["label"], row["cost_class"]) == (LOCAL_MODEL, "Fortissimo", "local")


@pytest.mark.asyncio
async def test_spend_groups_past_seven_with_the_models_cost_class(monkeypatch, by_model):
    """Spend's remainder comes from the one grouping owner, classed as Models classes the same models."""
    from api import costs
    from api.cost_identity import ModelIdentity
    from api.measurement_models import build_models

    shown: dict = {}
    real = costs.group_spend

    def spy(rows):
        shown["rows"] = real(rows)
        return shown["rows"]

    monkeypatch.setattr(costs, "group_spend", spy)
    await by_model(*[(f"vendor/model-{index}", 10.0 - index) for index in range(8)], (LOCAL_ID, None))
    rest = shown["rows"][-1]
    assert len(shown["rows"]) == 8
    assert (rest["model"], rest["label"], rest["other"]) == ("", "2 more models", True)
    assert (rest["cost_usd"], rest["api_calls"], rest["cost_class"]) == (3.0, 2, "priced")

    # The same two models in the Models table's floor row: priced plus local is priced there too.
    def cost(key: str, outcome: str, dollars: float | None) -> dict:
        identity = ModelIdentity(key, key, True)
        return {"period": "2026-09-28", "task_id": key, "identity": identity, "outcome": outcome, "dollars": dollars}

    built = build_models(
        [], [], [], [cost("vendor/model-7", "priced", 3.0), cost(LOCAL_MODEL, "not_api_priced", None)],
        resolve=lambda row: ModelIdentity(row["model"], row["model"], True),
        latest=("2026-09-28",), previous=("2026-09-21",), offered=set(),
        ledger_rows=[{"period": "2026-09-28", "model": "vendor/model-7", "outcome": "priced", "dollars": 3.0}],
    )
    assert built["rows"][-1]["cost_class"] == rest["cost_class"]


@pytest.mark.asyncio
async def test_a_legacy_external_placeholder_zero_reads_unpriced_not_free(by_model):
    rows = await by_model(
        ("external@srv-1/qwen-plus", 0.0), (LOCAL_ID, 0.0), ("claude-code/claude-opus-4-8", 2.0))
    classes = {row["model"]: row["cost_class"] for row in rows.values()}
    assert classes == {"external@srv-1/qwen-plus": "no_pricing", LOCAL_MODEL: "local",
                       "claude-code/claude-opus-4-8": "priced"}
