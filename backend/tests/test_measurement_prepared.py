"""Overlapping same-key Measurement requests share one outcome preparation; nothing outlives it."""

from __future__ import annotations

from datetime import datetime, timezone
import threading
import time

import pytest
from tests.plan_writer_authorizer import raw_plan_seed


def _grain(zone: str = "UTC", kind: str = "weeks", day: int = 21):
    """The page's grain with its last completed week starting on the given September Monday."""
    from core.measurement_scope import resolve_grain

    return resolve_grain(kind, zone, now=datetime(2026, 9, day + 7, 12, tzinfo=timezone.utc))


PARAMS = {"user_id": "steve", "scope_project": "one", "scope_agent": None, "grain": _grain(day=14)}


def _seed(db) -> None:
    for project in ("one", "two"):
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,'t','t')", (project, project))
        db.execute(
            "INSERT INTO threads (id,user_id,title,project_id,created_at,updated_at) "
            "VALUES (?,?,?,?,'2026-09-01T00:00:00Z','2026-09-01T00:00:00Z')",
            (f"root-{project}", "steve", project, project),
        )
        db.execute(
            "INSERT INTO plans (id,owner_kind,owner_id,created_at,updated_at) "
            "VALUES (?,'thread',?,'2026-09-01','2026-09-01')", (f"plan-{project}", f"root-{project}"),
        )
    db.commit()


def _accept(db, node_id: str, plan_id: str) -> None:
    with raw_plan_seed(db):
        db.execute(
            "INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,created_at,updated_at) "
            "VALUES (?,?,0,'task','t','c','accepted',?,'2026-09-15','2026-09-16')", (node_id, plan_id, node_id),
        )
        db.execute(
            "INSERT INTO plan_node_events (id,node_id,provenance,event,new_state,created_at) "
            "VALUES (?,?,'test','accepted','accepted','2026-09-16T10:00:00Z')", (f"ev-{node_id}", node_id),
        )
    db.commit()


def _prepare(params=PARAMS, *, roots: bool = False) -> list[dict]:
    from api.measurement_queries import prepare_outcome_periods, query_family
    from infra.database import get_db

    db = get_db()
    try:
        prepare_outcome_periods(db, params, roots=roots)
        return [row for row in query_family(db, "outcomes", params) if row["outcomes"] or row["period"] == "2026-09-14"]
    finally:
        db.close()


GATE_TIMEOUT = 20.0


def _count_builds(monkeypatch, *, gate: threading.Event | None = None, fail_first: bool = False) -> list[int]:
    """Count materializations; with a ``gate`` each one blocks until the test releases it."""
    from api import measurement_queries

    real, calls = measurement_queries._materialize_outcome_periods, []

    def counted(db, params, *, roots):
        calls.append(1)
        if gate is not None:
            assert gate.wait(GATE_TIMEOUT), "gate was never released"
        if fail_first and len(calls) == 1:
            raise RuntimeError("boom")
        return real(db, params, roots=roots)

    monkeypatch.setattr(measurement_queries, "_materialize_outcome_periods", counted)
    return calls


def _waiting() -> int:
    from api import measurement_prepared

    with measurement_prepared._flights_lock:
        return sum(flight.waiters for flight in measurement_prepared._flights.values())


def _until(condition) -> None:
    deadline = time.monotonic() + GATE_TIMEOUT
    while not condition():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.005)


def _overlapping(variants, *, roots=False, gate=None, release_when=None):
    """Run one ``_prepare`` per variant at once; release the gate once ``release_when()`` holds."""
    results: list = [None] * len(variants)
    errors: list = [None] * len(variants)

    def worker(i, params):
        try:
            results[i] = _prepare(params, roots=roots)
        except Exception as exc:  # reported to the test, which asserts on it
            errors[i] = exc

    threads = [threading.Thread(target=worker, args=(i, p)) for i, p in enumerate(variants)]
    [t.start() for t in threads]
    try:
        if gate is not None:
            _until(release_when)
    finally:
        if gate is not None:
            gate.set()
        [t.join() for t in threads]
    return results, errors


def _seeded(accepted=("n1",)):
    from infra.database import get_db

    db = get_db()
    _seed(db)
    for node in accepted:
        _accept(db, node, "plan-one")
    return db


def test_simultaneous_same_key_calls_compute_once(isolated_db, monkeypatch):
    _seeded().close()
    gate = threading.Event()
    calls = _count_builds(monkeypatch, gate=gate)
    results, errors = _overlapping([PARAMS] * 9, gate=gate, release_when=lambda: _waiting() == 8)
    assert errors == [None] * 9
    assert len(calls) == 1
    assert all(r == results[0] for r in results)
    assert results[0] == [{"period": "2026-09-14", "outcomes": 1, "first_try": 1, "rejections": 0}]


def test_shared_result_matches_direct_materialization(isolated_db, monkeypatch):
    from api.measurement_queries import _materialize_outcome_periods as direct, query_family

    db = _seeded()
    try:
        for roots in (False, True):
            gate = threading.Event()
            calls = _count_builds(monkeypatch, gate=gate)
            shared, errors = _overlapping([PARAMS] * 3, roots=roots, gate=gate, release_when=lambda: _waiting() == 2)
            assert errors == [None] * 3 and len(calls) == 1 and shared[0] == shared[1] == shared[2]
            direct(db, PARAMS, roots=roots)
            assert shared[0] == [row for row in query_family(db, "outcomes", PARAMS) if row["outcomes"]]
            if roots:
                assert [row for row in query_family(db, "outcomes", PARAMS, grouping="root") if row["outcomes"]] == [
                    {"root_id": "root-one", "period": "2026-09-14", "outcomes": 1, "first_try": 1}]
    finally:
        db.close()


def test_sequential_same_key_call_recomputes(isolated_db, monkeypatch):
    db = _seeded()
    calls = _count_builds(monkeypatch)
    assert _prepare()[0]["outcomes"] == 1
    _accept(db, "n2", "plan-one")
    db.close()
    assert _prepare()[0]["outcomes"] == 2
    assert _prepare()[0]["outcomes"] == 2
    assert len(calls) == 3


def test_errors_are_not_cached(isolated_db, monkeypatch):
    _seeded().close()
    calls = _count_builds(monkeypatch, fail_first=True)
    with pytest.raises(RuntimeError):
        _prepare()
    assert _prepare()[0]["outcomes"] == 1
    assert len(calls) == 2


def test_waiter_survives_a_failed_leader(isolated_db, monkeypatch):
    _seeded().close()
    gate = threading.Event()
    calls = _count_builds(monkeypatch, gate=gate, fail_first=True)
    results, errors = _overlapping([PARAMS] * 2, gate=gate, release_when=lambda: _waiting() == 1)
    failed = [e for e in errors if e is not None]
    assert len(failed) == 1 and isinstance(failed[0], RuntimeError)
    # The waiter was inside shared_prepare when the leader failed, and ran its own materialization.
    assert len(calls) == 2
    assert [r for r in results if r is not None] == [[{"period": "2026-09-14", "outcomes": 1, "first_try": 1, "rejections": 0}]]


def test_leader_without_waiters_takes_no_snapshot(isolated_db, monkeypatch):
    from api import measurement_prepared

    _seeded().close()
    taken = []
    real = measurement_prepared._snapshot
    monkeypatch.setattr(measurement_prepared, "_snapshot", lambda db, roots: taken.append(1) or real(db, roots))
    assert _prepare()[0]["outcomes"] == 1
    assert taken == []


def test_different_keys_do_not_collide(isolated_db, monkeypatch):
    _seeded().close()
    gate = threading.Event()
    calls = _count_builds(monkeypatch, gate=gate)
    variants = [PARAMS, {**PARAMS, "scope_project": "two"}, {**PARAMS, "user_id": "someone-else"},
                {**PARAMS, "grain": _grain(day=21)}, {**PARAMS, "scope_agent": "root-one"},
                {**PARAMS, "grain": _grain("America/Los_Angeles", day=14)}]
    # Every variant must reach its own materialization while all are still in flight.
    results, errors = _overlapping(variants, gate=gate, release_when=lambda: len(calls) == len(variants))
    assert errors == [None] * len(variants) and len(calls) == len(variants)
    assert results[0][0]["outcomes"] == 1
    assert results[1:3] == [[], []]
    # A later last period still reaches back over the accepted one; another zone is its own preparation.
    assert results[3] == results[5] == results[0]
