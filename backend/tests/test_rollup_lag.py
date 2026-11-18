"""A refresh that leaves a Measurement rollup behind says how far, so slow raw reads have a visible cause."""
from __future__ import annotations

from datetime import datetime

import pytest

NOW = datetime(2026, 10, 8, 12, 30)


def _reader(coverage):
    async def read(job):
        assert job.__name__ == "coverage_job"
        return coverage
    return read


async def _warnings(caplog, read) -> list[str]:
    from api import rollup_lag

    with caplog.at_level("WARNING", logger="api.rollup_lag"):
        await rollup_lag.report_lag(read, "completed", {"usage": 1, "presence": 1})
    return [r.getMessage() for r in caplog.records if r.name == "api.rollup_lag"]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from api import rollup_lag

    monkeypatch.setattr(rollup_lag, "utc_now", lambda: NOW)
    monkeypatch.setattr(rollup_lag, "_last_warning", [float("-inf")])


@pytest.mark.asyncio
@pytest.mark.parametrize(("coverage", "warned"), [
    (("2026-10-08T10", 0, "2026-10-08 10:00:00", ""), False),  # both current
    (("2026-10-08T10", 0, "2026-10-01 10:00:00", "2026-10-01T09:58:00"), True),  # focus held a week back
    (("", 0, "2026-10-08 10:00:00", ""), True),  # usage not covered at all
    (("2026-10-08T10", 3, "", ""), True),  # focus reset or never built
])
async def test_a_rollup_left_behind_says_how_far(caplog, coverage, warned):
    lines = await _warnings(caplog, _reader(coverage))
    assert bool(lines) is warned
    if warned:
        assert f"usage built_through={coverage[0] or 'none'}" in lines[0]
        assert f"presence built_through={coverage[2] or 'none'}" in lines[0]
        assert f"dirty_hours={coverage[1]}" in lines[0]
        assert f"oldest_open_visit={coverage[3] or 'none'}" in lines[0]


@pytest.mark.asyncio
async def test_a_lagging_rollup_warns_at_most_every_fifteen_minutes(caplog):
    read = _reader(("2026-10-08T10", 0, "2026-10-01 10:00:00", ""))
    first = await _warnings(caplog, read)
    second = await _warnings(caplog, read)
    assert len(first) == 1 and len(second) == 1  # the second call added nothing
    assert "presence built_through=2026-10-01 10:00:00 hours_behind=170.5 " in first[0]


@pytest.mark.asyncio
async def test_the_coverage_read_runs_on_the_bulk_lane_against_the_real_schema(isolated_db, caplog):
    from api import presence_rollup
    from infra.database import get_db
    from infra.db_async import run_bulk

    db = get_db()
    db.execute("INSERT INTO presence_focus_rollup_state(id,definition,built_through) VALUES (1,?,?) "
               "ON CONFLICT(id) DO UPDATE SET definition=excluded.definition,built_through=excluded.built_through",
               (presence_rollup.DEFINITION, "2026-10-01 10:00:00"))
    db.commit()
    lines = await _warnings(caplog, run_bulk)
    assert len(lines) == 1
    assert "presence built_through=2026-10-01 10:00:00 hours_behind=170.5 " in lines[0]
    assert "usage built_through=none hours_behind=all" in lines[0]


@pytest.mark.asyncio
async def test_a_failed_coverage_read_is_reported_not_raised(caplog):
    async def read(job):
        raise RuntimeError("database locked")

    lines = await _warnings(caplog, read)
    assert lines == ["rollup coverage read failed"]
