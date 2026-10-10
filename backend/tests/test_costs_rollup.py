"""Spending serves the same figures from the hourly usage rollup as from the raw ledger."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

THREADS: tuple[tuple[str, str | None, str | None, int], ...] = (
    ("agent", "project", None, 1),
    ("agent-child", None, "agent", 0),
    ("peer", "project", None, 0),
    ("other", "elsewhere", None, 0),
)
# One clock for both reads, mid-day and mid-month, so today, this month and the day windows never
# move between the raw read and the rollup read. 12:30 also ends ``days=N`` inside an hour.
NOW = datetime(2026, 10, 14, 12, 30)


def _rounded(value):
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rounded(item) for item in value]
    return value


def _seed(db, rng: random.Random, now) -> None:
    for project in ("project", "elsewhere"):
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,'t','t')", (project, project))
    for thread_id, project_id, parent_id, agent in THREADS:
        db.execute(
            "INSERT INTO threads (id,user_id,title,project_id,parent_thread_id,agent_enabled,agent_status,created_at,updated_at) "
            "VALUES (?, 'steve', ?, ?, ?, ?, 'active', 't', 't')",
            (thread_id, thread_id, project_id, parent_id, agent),
        )
    # Spread over 13 weeks up to now: some rows land in the raw tail, today and this month.
    for index in range(800):
        created = now - timedelta(minutes=rng.randrange(60 * 24 * 91))
        p, c = rng.randrange(0, 50_000), rng.randrange(0, 5_000)
        db.execute(
            "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,total_tokens,cost_usd,source,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"u{index}", rng.choice([row[0] for row in THREADS] + ["unscoped"]),
             rng.choice(("model-a", "model-b", "ollama/local")), rng.choice((p, None)), c, rng.choice((p + c, None)),
             rng.choice((None, rng.random())), rng.choice(("turn", "checkpoint", "summary")), created.isoformat()),
        )
    db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(("project", "agent", "window"), [
    ("all", "none", {"range": "12w"}),
    ("project", "none", {"range": "12w"}),
    ("all", "agent", {"range": "26w"}),
    ("all", "none", {"days": 10}),  # ends now, inside an hour
    ("all", "none", {"days": 1}),
])
async def test_spending_from_the_rollup_equals_the_raw_ledger(isolated_db, monkeypatch, project, agent, window):
    from api import costs
    from api.usage_rollup import refresh_step, rollup_through
    from infra.database import get_db

    monkeypatch.setattr(costs, "authenticate", lambda _: {"id": "steve"})
    monkeypatch.setattr(costs, "schedule_refresh", lambda: None)
    monkeypatch.setattr(costs, "utc_now", lambda: NOW)
    db = get_db()
    _seed(db, random.Random(17), NOW)
    raw = _rounded(await costs.cost_summary("t", project=project, agent=agent, **window))
    raw_period = _rounded([dict(row) for row in costs._period_totals(db)])
    while refresh_step(db, NOW):
        pass
    assert rollup_through(db)
    assert db.execute("SELECT COUNT(*) FROM llm_usage_hourly").fetchone()[0] > 0
    built = _rounded(await costs.cost_summary("t", project=project, agent=agent, **window))
    assert built == raw
    assert raw["totals"]["api_calls"] > 0
    assert _rounded([dict(row) for row in costs._period_totals(db)]) == raw_period


def test_a_window_inside_an_hour_never_reads_that_hour_from_the_rollup(isolated_db):
    from api.usage_rollup import definition, rollup_bound
    from infra.database import get_db

    db = get_db()
    db.execute("INSERT INTO llm_usage_hourly_state(id,definition,built_through) VALUES (1,?,?)",
               (definition(), "2026-10-08T10"))
    assert rollup_bound(db, "2026-10-01", "2026-10-09") == "2026-10-08T10"
    assert rollup_bound(db, "2026-10-01", "2026-10-08T09:30:00") == "2026-10-08T09"
    assert rollup_bound(db, "2026-10-01T04:15:00", "2026-10-09") == ""
