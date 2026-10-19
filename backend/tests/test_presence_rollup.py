"""The focus-attention rollup answers exactly what the raw spans do, and never serves a stale bucket."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

import pytest

from api import presence_rollup
from tests.test_measurement_presence_scope import LEGACY_WEEK_FROM, LEGACY_WEEK_TO, _seed_thread

START = datetime(2026, 9, 14, 0, 0, 0)
FAR = datetime(2026, 12, 31)
SCOPES = (("all", None), ("one", None), ("all", "a"), ("two", "c"))


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds")


def _visit(db, thread: str | None, entered: datetime, exited: datetime, spans: str | None, *, project: str | None = None,
           user: str = "steve") -> None:
    db.execute(
        "INSERT INTO presence_visits (user_id,surface,thread_id,project_id,entered_at,exited_at,seconds,sessions,"
        "exit_reason,focus_spans) VALUES (?,'chat',?,?,?,?,?,1,'navigated',?)",
        (user, thread, project, _stamp(entered), _stamp(exited), int((exited - entered).total_seconds()), spans),
    )


def _random_visit(db, rng: random.Random) -> None:
    thread = rng.choice(["a", "b", "c", "d", None])
    entered = START + timedelta(seconds=rng.randrange(0, 7 * 86400 - 3600))
    exited = entered + timedelta(seconds=rng.randrange(60, 4 * 3600))
    spans: str | None = None
    if rng.random() > 0.2:
        pairs: list[list[str]] = []
        cursor = entered - timedelta(seconds=rng.randrange(0, 600))
        while cursor < exited and len(pairs) < 40:
            began = cursor + timedelta(seconds=rng.randrange(0, 900), microseconds=rng.randrange(1_000_000))
            ended = began + timedelta(seconds=rng.randrange(1, 1500), microseconds=rng.randrange(1_000_000))
            pairs.append([_stamp(began), _stamp(ended)])
            cursor = ended
        spans = json.dumps(pairs)
    project = rng.choice(["one", "two"]) if thread is None else {"a": "one", "b": "one", "d": "one", "c": "two"}[thread]
    _visit(db, thread, entered, exited, spans, project=project)


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    conn = get_db()
    try:
        _seed_thread(conn, "a", project_id="one", agent=True)
        _seed_thread(conn, "b", project_id="one", agent=True)
        _seed_thread(conn, "c", project_id="two", agent=True)
        _seed_thread(conn, "d", project_id="one", parent_id="a")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _params(scope_project: str, scope_agent: str | None) -> dict:
    from tests.test_measurement_api import _grained

    return _grained({"user_id": "steve", "scope_project": scope_project, "scope_agent": scope_agent,
                     "from": LEGACY_WEEK_FROM, "to": LEGACY_WEEK_TO})


def _answers(db) -> dict:
    """Every presence answer Measurement serves, for each scope and the agents table."""
    from api.measurement_agent_attention import query_root_active
    from api.measurement_queries import prepare_outcome_periods, query_family

    answers: dict = {}
    for project, agent in SCOPES:
        params = _params(project, agent)
        prepare_outcome_periods(db, params)
        for family in ("presence", "active"):
            answers[(project, agent, family)] = [dict(row) for row in query_family(db, family, params)]
    params = _params("all", None)
    prepare_outcome_periods(db, params, roots=True)
    answers["roots"] = [dict(row) for row in query_root_active(db, params)]
    return answers


def _build(db, now: datetime = FAR) -> None:
    while presence_rollup.refresh_step(db, now):
        pass
    db.commit()


def _assert_same(raw: dict, rolled: dict) -> None:
    assert raw.keys() == rolled.keys()
    for key in raw:
        assert len(raw[key]) == len(rolled[key]), key
        for before, after in zip(raw[key], rolled[key]):
            assert before.keys() == after.keys(), key
            for column, value in before.items():
                assert after[column] == pytest.approx(value, abs=1e-6), (key, column)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_rollup_answers_what_the_spans_do(db, seed):
    rng = random.Random(seed)
    for _ in range(120):
        _random_visit(db, rng)
    db.commit()
    raw = _answers(db)
    assert any(row["value"] for rows in raw.values() if isinstance(rows, list) for row in rows if "value" in row)

    _build(db)
    params = _params("all", None)
    assert presence_rollup.rollup_binds(db, params), "the rollup must serve this read"
    _assert_same(raw, _answers(db))


def test_a_partly_built_rollup_reads_the_spans_for_the_rest(db):
    rng = random.Random(7)
    for _ in range(80):
        _random_visit(db, rng)
    db.commit()
    raw = _answers(db)
    _build(db, START + timedelta(days=3, hours=2, minutes=40))  # three days and a bit; the rest is the raw tail
    binds = presence_rollup.rollup_binds(db, _params("all", None))
    assert binds["focus_rollup_through"] == "2026-09-17 00:30:00"
    _assert_same(raw, _answers(db))


def test_an_open_visit_holds_the_rollup_back_and_counts_through_the_spans(db):
    _visit(db, "a", START + timedelta(hours=1), START + timedelta(hours=2), "[]")
    db.execute(
        "INSERT INTO thread_presence (user_id,thread_id,present,last_entered_at,last_confirmed_at,updated_at,"
        "exit_pending_at,focus_spans,focus_since) VALUES ('steve','b',1,?,?,?,?,'[]',?)",
        (_stamp(START + timedelta(hours=5, minutes=7)),) * 3
        + (_stamp(START + timedelta(hours=6)), _stamp(START + timedelta(hours=5, minutes=10))),
    )
    db.commit()
    raw = _answers(db)
    _build(db)
    assert presence_rollup.rollup_binds(db, _params("all", None))["focus_rollup_through"] == "2026-09-14 05:00:00"
    _assert_same(raw, _answers(db))


def test_a_visit_written_under_the_rollup_moves_it_back_and_is_counted_after_a_rebuild(db):
    _visit(db, "a", START + timedelta(hours=1), START + timedelta(hours=2), None)
    _build(db)
    assert db.execute("SELECT built_through FROM presence_focus_rollup_state").fetchone()[0] == "2026-12-30 22:00:00"
    late = START + timedelta(days=1, hours=3, minutes=20)
    _visit(db, "b", late, late + timedelta(minutes=30), None)  # a late write, far below built_through
    assert db.execute("SELECT built_through FROM presence_focus_rollup_state").fetchone()[0] == "2026-09-15 03:15:00"
    raw_hours = sum(row["value"] for row in _answers(db)[("all", None, "presence")])
    assert raw_hours == pytest.approx(1.5)  # the stale rows above the new bound are never read
    db.commit()
    _build(db)
    assert db.execute("SELECT built_through FROM presence_focus_rollup_state").fetchone()[0] == "2026-12-30 22:00:00"
    assert sum(row["value"] for row in _answers(db)[("all", None, "presence")]) == pytest.approx(1.5)


def test_a_deleted_visit_drops_out_of_the_rollup_after_the_rebuild(db):
    _visit(db, "a", START + timedelta(hours=1), START + timedelta(hours=2), None)
    _visit(db, "b", START + timedelta(hours=10), START + timedelta(hours=12), None)
    _build(db)
    db.execute("DELETE FROM presence_visits WHERE thread_id='b'")
    _build(db)
    assert sum(row["value"] for row in _answers(db)[("all", None, "presence")]) == pytest.approx(1.0)


def test_a_period_that_starts_between_buckets_reads_the_spans(db):
    from core.measurement_scope import resolve_grain

    _visit(db, "a", START + timedelta(hours=1), START + timedelta(hours=2), None)
    _build(db)
    params = _params("all", None)
    assert presence_rollup.rollup_binds(db, params)
    params["grain"] = resolve_grain("days", "Asia/Kathmandu", now=datetime(2026, 9, 21, 12))  # UTC+05:45: on a bucket
    assert presence_rollup.rollup_binds(db, params)
    params["grain"] = resolve_grain("days", "Australia/Eucla", now=datetime(2026, 9, 21, 12))  # UTC+08:45: on a bucket
    assert presence_rollup.rollup_binds(db, params)
    params["from_ts"] = "2026-09-14 00:05"
    assert presence_rollup.rollup_binds(db, params) == {}


def test_a_definition_change_rebuilds_from_nothing(db, monkeypatch):
    _visit(db, "a", START + timedelta(hours=1), START + timedelta(hours=2), None)
    _build(db)
    assert db.execute("SELECT COUNT(*) FROM presence_focus_rollup").fetchone()[0] > 0
    monkeypatch.setattr(presence_rollup, "DEFINITION", "next")
    assert presence_rollup.rollup_binds(db, _params("all", None)) == {}  # never served under another definition
    _build(db)
    assert presence_rollup.rollup_binds(db, _params("all", None))
    assert sum(row["value"] for row in _answers(db)[("all", None, "presence")]) == pytest.approx(1.0)


def test_a_focus_heavy_history_is_a_few_rows_a_day(db):
    day = START + timedelta(hours=9)
    pairs = [[_stamp(day + timedelta(seconds=n * 324)), _stamp(day + timedelta(seconds=n * 324 + 178))] for n in range(100)]
    _visit(db, "a", day, day + timedelta(hours=9), json.dumps(pairs), project="one")
    _build(db)
    rows = db.execute("SELECT COUNT(*) FROM presence_focus_rollup").fetchone()[0]
    assert rows == 36  # one set, 36 buckets in nine hours, however often the window was left
    hours = sum(row["value"] for row in _answers(db)[("all", None, "presence")])
    assert hours == pytest.approx(100 * 178 / 3600)


def test_a_covered_read_is_served_from_the_rollup_rows(db):
    """The equality above is only meaningful if the covered time really comes from the rollup."""
    rng = random.Random(5)
    for _ in range(40):
        _random_visit(db, rng)
    _build(db)
    before = _answers(db)
    db.execute("UPDATE presence_focus_rollup SET seconds=seconds*2")
    after = _answers(db)
    assert after[("all", None, "presence")] != before[("all", None, "presence")]
    assert after["roots"] != before["roots"]


def test_set_ids_follow_the_stored_history_not_the_process(db):
    """A set's id decides the order the read joins in, so the same history must build the same ids on every run."""
    rng = random.Random(9)
    for _ in range(40):
        _random_visit(db, rng)
    _build(db)
    signatures = [row[0] for row in db.execute("SELECT signature FROM presence_focus_sets ORDER BY id")]
    assert len(signatures) > 3
    assert signatures == sorted(signatures)
