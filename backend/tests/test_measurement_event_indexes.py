"""Schema 189: the plan-event indexes Measurement settles from change no figure, only how it is read.

* Every Measurement panel serves the same body with the indexes as with the statements they replaced
  (the first-outcome start probes pinned to the full created-at index, and no partial index at all).
* The settlement and the correction and send-back sets read the partial indexes, not the whole
  event history. Plans are read with no planner statistics, as production never runs ANALYZE.
* A migrated database carries the same indexes as a fresh schema.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from infra.database import initialize_schema
from infra.measurement_schema import PLAN_EVENT_MEASUREMENT_INDEXES
from infra.migration_188_to_189_measurement_event_indexes import migrate_188_to_189
from tests.test_measurement_settle_once import JOBS, _client, _seed

_NEW = ("idx_measurement_accepted_node", "idx_measurement_accepted_time",
        "idx_measurement_corrections_node", "idx_measurement_rejected_node")
_QUERY = "grain=weeks&timezone=UTC&project=all&agent=none"
ROUTES = {
    **JOBS,
    "series": f"/api/measurement/series?ids=outcomes,first_try_acceptance,rejections_per_outcome,fresh_tokens_per_outcome&{_QUERY}",
    "key-figures": f"/api/measurement/key-figures?{_QUERY}",
    "where-to-look": f"/api/measurement/where-to-look?ids=outcomes,first_try_acceptance&{_QUERY}",
    "workflows": f"/api/measurement/workflows?{_QUERY}",
}


def _without_clock(value):
    """The body with the one wall-clock field it carries removed; every other byte is compared."""
    if isinstance(value, dict):
        return {key: _without_clock(item) for key, item in value.items() if key != "generated_at"}
    if isinstance(value, list):
        return [_without_clock(item) for item in value]
    return value


def _bodies() -> dict[str, tuple[int, str]]:
    client = _client()
    served = {name: client.get(url) for name, url in ROUTES.items()}
    return {name: (response.status_code, json.dumps(_without_clock(response.json()), sort_keys=False))
            for name, response in served.items()}


def test_every_panel_serves_the_same_body_without_the_partial_indexes(isolated_db, measurement_clock, monkeypatch):
    from infra import measurement_starts
    from infra.database import get_db

    _seed()
    new = _bodies()
    assert any(status == 200 for status, _ in new.values())

    db = get_db()
    try:
        for name in _NEW:
            db.execute(f"DROP INDEX {name}")
        db.commit()
    finally:
        db.close()
    for source in ("accepted_outcomes", "accepted_size_ratings"):
        monkeypatch.setitem(measurement_starts._FIRST_ROW_SQL, source, measurement_starts._FIRST_ROW_SQL[source]
                            .replace("idx_measurement_accepted_time", "idx_plan_node_events_created"))
    assert _bodies() == new


def test_settlement_and_event_sets_read_the_partial_indexes(isolated_db, measurement_clock):
    from api.measurement_queries import outcome_ctes
    from core.measurement_scope import query_params, resolve_grain, scope_cte
    from datetime import datetime, timezone
    from infra.database import get_db

    _seed()
    grain = resolve_grain("weeks", "UTC", now=datetime(2026, 9, 29, 12, tzinfo=timezone.utc))
    bound = query_params({"user_id": "steve", "scope_project": "all", "scope_agent": None, "grain": grain})
    db = get_db()
    try:
        plan = " ".join(str(row[-1]) for row in db.execute(
            "EXPLAIN QUERY PLAN " + scope_cte() + outcome_ctes(grain) + "SELECT id FROM outcome_rows", bound))
    finally:
        db.close()
    for name in ("idx_measurement_accepted_node", "idx_measurement_corrections_node"):
        assert name in plan
    assert "idx_plan_node_events_node" not in plan


@pytest.mark.parametrize("source", ["accepted_outcomes", "accepted_size_ratings"])
def test_first_outcome_probes_seek_the_accepted_time_index(isolated_db, source):
    from infra.database import get_db
    from infra.measurement_starts import source_first_row_sql

    db = get_db()
    try:
        plan = " ".join(str(row[-1]) for row in db.execute("EXPLAIN QUERY PLAN " + source_first_row_sql(source)))
    finally:
        db.close()
    assert "idx_measurement_accepted_time" in plan


def test_a_migrated_database_carries_the_indexes_a_fresh_one_has():
    def names(db):
        return {row[0]: row[1] for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' AND name LIKE 'idx_measurement_%'")}

    with closing(sqlite3.connect(":memory:")) as fresh, closing(sqlite3.connect(":memory:")) as old:
        initialize_schema(fresh)
        initialize_schema(old)
        for name in _NEW:
            old.execute(f"DROP INDEX {name}")
        assert set(names(old)) | set(_NEW) == set(names(fresh))
        migrate_188_to_189(old)
        migrate_188_to_189(old)  # idempotent
        assert names(old) == names(fresh)
    assert len(PLAN_EVENT_MEASUREMENT_INDEXES) == len(_NEW)
