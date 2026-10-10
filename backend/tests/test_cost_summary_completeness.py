"""Cost views expose stored known dollars alongside unpriced calls."""

import pytest


@pytest.mark.asyncio
async def test_summary_does_not_hide_unknown_rows_or_reprice_history(isolated_db, monkeypatch):
    from api import costs
    from infra.database import get_db
    from infra.time import utc_now_iso

    monkeypatch.setattr(costs, "authenticate", lambda _: {"id": "user"})
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at) "
            "VALUES ('thread','user','Cost test','t','t')"
        )
        for index, cost in enumerate((0.25, None, 0.0)):
            db.execute("INSERT INTO llm_usage (id,thread_id,model,total_tokens,cost_usd,source,created_at) "
                       "VALUES (?, 'thread', 'unregistered-model', 150, ?, 'checkpoint', ?)",
                       (f"usage-{index}", cost, utc_now_iso()))
        db.commit()
    finally:
        db.close()
    summary = await costs.cost_summary("test-token", days=7)
    assert summary["totals"]["cost_usd"] == 0.25
    assert summary["totals"]["unknown_cost_rows"] == 1
    for key in ("weekly", "by_model", "by_source", "by_project", "top_threads"):
        assert summary[key][0]["unknown_cost_rows"] == 1
    assert summary["by_model"][0]["cost_class"] == "priced"
    assert summary["period_spend"]["today_unknown_cost_rows"] == 1
    assert summary["period_spend"]["monthly_unknown_cost_rows"] == 1
