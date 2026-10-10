"""A series serves its two comparison windows and every row's figure before the minimum-n rule."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.test_measurement_api import USER, _seed_thread

# Noon UTC on Friday 2026-10-09: the latest 7 days are 10-02..10-08 and the 7 before are 09-25..10-01.
NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def _accept(db, node_id: str, accepted_at: str) -> None:
    db.execute(
        "INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at) "
        "VALUES (?, 'plan-root', NULL, 1, 'task', 'task', 'done', 'accepted', ?, 1, 1, 't', 't')", (node_id, node_id),
    )
    db.execute(
        "INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,created_at) "
        "VALUES (?,?,'test','accepted','active','accepted',?)", (f"event-{node_id}", node_id, accepted_at),
    )


def _seed(days: dict[str, int]) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        db.execute("INSERT INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES ('plan-root','thread','root','active',1,'t','t')")
        for day, count in days.items():
            for index in range(count):
                _accept(db, f"{day}-{index}", f"{day}T12:00:00Z")
        db.commit()
    finally:
        db.close()


async def _series(ids: str, grain: str = "days", zone: str = "UTC") -> dict:
    from api.measurement import measurement_series

    return await measurement_series(ids, project="all", agent="none", grain=grain, timezone_name=zone, user=USER)


@pytest.mark.asyncio
async def test_a_row_under_the_minimum_keeps_its_raw_value_and_withholds_the_value(isolated_db, monkeypatch):
    monkeypatch.setattr("core.measurement_scope._now", lambda: NOW)
    _seed({"2026-10-03": 10, "2026-10-05": 15})

    rows = {row["period"]: row for row in (await _series("first_try_acceptance"))["rows"]}
    assert (rows["2026-10-03"]["n"], rows["2026-10-03"]["value"], rows["2026-10-03"]["raw_value"]) == (10, None, 1.0)
    assert (rows["2026-10-05"]["n"], rows["2026-10-05"]["value"], rows["2026-10-05"]["raw_value"]) == (15, None, 1.0)
    # A day nothing was accepted on carries no figure at all, not a zero.
    assert (rows["2026-10-04"]["n"], rows["2026-10-04"]["value"], rows["2026-10-04"]["raw_value"]) == (0, None, None)


@pytest.mark.asyncio
async def test_raw_value_is_the_same_in_a_single_and_a_batched_request(isolated_db, monkeypatch):
    monkeypatch.setattr("core.measurement_scope._now", lambda: NOW)
    _seed({"2026-10-03": 10, "2026-10-05": 15})

    single = (await _series("first_try_acceptance"))["rows"]
    batched = [row for row in (await _series("outcomes,first_try_acceptance,fresh_tokens"))["rows"]
               if row["series"] == "first_try_acceptance"]
    assert single == batched and all("raw_value" in row for row in single)


@pytest.mark.asyncio
async def test_windows_pool_each_side_from_its_raw_counts_and_apply_the_minimum_to_the_pool(isolated_db, monkeypatch):
    monkeypatch.setattr("core.measurement_scope._now", lambda: NOW)
    # Each day alone is under the minimum of 20; the latest seven days pool to 25 and the seven before to 12.
    _seed({"2026-10-03": 10, "2026-10-05": 15, "2026-09-28": 12})

    served = await _series("outcomes,first_try_acceptance")
    assert served["windows"]["outcomes"] == {"latest": {"value": 25, "n": 25}, "previous": {"value": 12, "n": 12}}
    first_try = served["windows"]["first_try_acceptance"]
    assert first_try["latest"] == {"value": 1.0, "n": 25}
    assert first_try["previous"] == {"value": None, "n": 12}, "under the minimum the value is withheld but n is reported"


@pytest.mark.asyncio
async def test_windows_follow_the_grain_four_periods_against_the_four_before(isolated_db, monkeypatch):
    monkeypatch.setattr("core.measurement_scope._now", lambda: NOW)
    # Weeks run Monday to Sunday and the week of 10-05 is still in progress: the latest four are
    # 09-07..10-04, the four before are 08-10..09-06, so Sunday 09-06 is the last day of the older side.
    _seed({"2026-10-03": 5, "2026-09-15": 7, "2026-09-07": 3, "2026-09-06": 2, "2026-08-20": 4})

    served = await _series("outcomes", grain="weeks")
    assert served["windows"]["outcomes"] == {"latest": {"value": 15, "n": 15}, "previous": {"value": 6, "n": 6}}


@pytest.mark.asyncio
async def test_a_window_reaching_before_the_instrument_pools_only_the_periods_it_measured(isolated_db, monkeypatch):
    # first_try_acceptance is measured from 2026-09-14: the earlier days of a window are not pooled as zeros.
    monkeypatch.setattr("core.measurement_scope._now", lambda: datetime(2026, 9, 20, 12, tzinfo=timezone.utc))
    _seed({"2026-09-12": 30, "2026-09-16": 20})

    served = await _series("first_try_acceptance")
    assert served["windows"]["first_try_acceptance"]["latest"] == {"value": 1.0, "n": 20}


@pytest.mark.asyncio
@pytest.mark.parametrize("grain", ["days", "weeks"])
async def test_every_available_series_serves_both_windows_on_both_grains(isolated_db, monkeypatch, grain):
    from api.measurement_registry import REGISTRY

    monkeypatch.setattr("core.measurement_scope._now", lambda: NOW)
    _seed({"2026-10-03": 10})
    ids = [name for name, item in REGISTRY.items() if item.available]

    served = await _series(",".join(ids), grain=grain)
    assert set(served["windows"]) == set(ids)
    for series, figures in served["windows"].items():
        assert set(figures) == {"latest", "previous"}
        # A size window also serves the counts behind its rate, so no reader re-pools them.
        keys = {"value", "n", "size"} if series == "estimated_human_hours_per_active_hour" else {"value", "n"}
        assert all(set(side) == keys for side in figures.values())


def _size_row(period: str, value: float | None, *, n: int = 30, raw: float | None = None, comparable: bool = True) -> dict:
    return {"period": period, "value": value, "n": n, "raw_value": raw, "comparable": comparable}


def test_a_size_window_pools_only_the_run_the_chart_draws():
    from api.measurement_size import size_window_periods

    # The run starts after the last period that is not comparable, then at its first period with a figure.
    rows = [_size_row("2026-09-07", 40), _size_row("2026-09-14", 41, comparable=False), _size_row("2026-09-21", None, n=0),
            _size_row("2026-09-28", None, n=5, raw=90), _size_row("2026-10-05", 44)]
    assert size_window_periods(rows) == {"2026-09-28", "2026-10-05"}
    # A latest period that is itself not comparable stands alone.
    assert size_window_periods([_size_row("2026-09-28", 40), _size_row("2026-10-05", 44, comparable=False)]) == {"2026-10-05"}
    # A run with no figure at all pools nothing.
    assert size_window_periods([_size_row("2026-09-28", None, n=0), _size_row("2026-10-05", None, n=0)]) == set()
    assert size_window_periods([]) == set()
