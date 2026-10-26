"""Corrections per outcome and asks plus corrections per outcome, through the weekly series route."""

from __future__ import annotations

import json

import pytest

from tests.test_measurement_api import USER, WEEK, _freeze_clock, _seed_decision, _seed_outcomes, _seed_thread

IDS = "corrections_per_outcome,asks_plus_corrections_per_outcome,asks_per_outcome"


def _fact(db, source_id: str, *, thread: str = "root", at: str = "2026-09-22T12:00:00Z", label: str = "correction",
          version: str = "v1", classified_at: str = "2026-10-01T00:00:00Z", retracted: bool = False,
          kind: str = "message") -> None:
    """One persisted owner message and its classifier fact, the shape the recording job writes."""
    db.execute("INSERT OR IGNORE INTO messages (id,thread_id,role,content,created_at) VALUES (?,?,'user','x',?)",
               (source_id, thread, at))
    key = f"{kind}:{source_id}"
    db.execute(
        "INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,"
        "agent_thread_id,attributes,retracted_at) VALUES (?,?,'measurement.correction','corsia','instrumented',?,?,?,?,?,?)",
        (f"{key}:{version}", f"{key}:{version}", at, at, key, thread,
         json.dumps({"label": label, "asset_version": version, "classified_at": classified_at}),
         "2026-10-02" if retracted else None),
    )


async def _rows(monkeypatch, ids: str = IDS) -> dict[tuple[str, str], dict]:
    from api.measurement import measurement_series

    _freeze_clock(monkeypatch)
    result = await measurement_series(ids, project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    return {(row["series"], row["period"]): row for row in result["rows"]}


def _seed(db, outcomes: int = 20) -> None:
    _seed_thread(db, "root")
    _seed_outcomes(db, "root", count=outcomes)


@pytest.mark.asyncio
async def test_corrections_and_asks_are_counted_per_outcome(isolated_db, monkeypatch):
    from infra.database import get_db

    db = get_db()
    try:
        _seed(db)
        for number in range(3):
            _fact(db, f"m{number}")
        _fact(db, "not-one", label="not_correction")
        _fact(db, "retracted", retracted=True)
        _fact(db, "elsewhere", at="2026-09-01T12:00:00Z")  # another week
        _seed_thread(db, "foreign", user_id="another-user")
        _fact(db, "foreign-message", thread="foreign")  # another person's thread is outside the scope
        for index in range(2):
            _seed_decision(db, index, "A", raised="2026-09-22T12:00:00Z")
        db.commit()
    finally:
        db.close()

    rows = await _rows(monkeypatch)
    assert rows[("corrections_per_outcome", WEEK)]["value"] == pytest.approx(3 / 20)
    assert rows[("asks_per_outcome", WEEK)]["value"] == pytest.approx(2 / 20)
    assert rows[("asks_plus_corrections_per_outcome", WEEK)]["value"] == pytest.approx(5 / 20)
    assert rows[("corrections_per_outcome", WEEK)]["n"] == 20
    assert rows[("corrections_per_outcome", WEEK)]["method_version"] == 1


@pytest.mark.asyncio
async def test_a_period_with_no_corrections_is_a_measured_zero_once_corrections_are_recorded(isolated_db, monkeypatch):
    from infra.database import get_db

    db = get_db()
    try:
        _seed(db)
        _fact(db, "earlier", at="2026-09-08T12:00:00Z")
        db.commit()
    finally:
        db.close()

    rows = await _rows(monkeypatch)
    assert rows[("corrections_per_outcome", WEEK)]["value"] == 0
    assert rows[("asks_plus_corrections_per_outcome", WEEK)]["value"] == 0


@pytest.mark.asyncio
async def test_no_recorded_corrections_means_not_measured_never_zero(isolated_db, monkeypatch):
    from infra.database import get_db

    db = get_db()
    try:
        _seed(db)
        db.commit()
    finally:
        db.close()

    rows = await _rows(monkeypatch)
    assert not [key for key in rows if key[0] in {"corrections_per_outcome", "asks_plus_corrections_per_outcome"}]
    assert rows[("asks_per_outcome", WEEK)]["value"] == 0  # the instrument that exists is untouched


@pytest.mark.asyncio
async def test_a_source_classified_under_two_versions_counts_once_by_its_latest(isolated_db, monkeypatch):
    from infra.database import get_db

    db = get_db()
    try:
        _seed(db)
        _fact(db, "twice", version="v1", classified_at="2026-10-01T00:00:00Z")
        _fact(db, "twice", version="v2", classified_at="2026-10-02T00:00:00Z")
        _fact(db, "withdrawn", version="v1", classified_at="2026-10-01T00:00:00Z")
        _fact(db, "withdrawn", version="v2", classified_at="2026-10-02T00:00:00Z", label="not_correction")
        db.commit()
    finally:
        db.close()

    rows = await _rows(monkeypatch)
    assert rows[("corrections_per_outcome", WEEK)]["value"] == pytest.approx(1 / 20)


@pytest.mark.asyncio
async def test_a_sent_back_review_is_not_a_person_correction(isolated_db, monkeypatch):
    from infra.database import get_db

    db = get_db()
    try:
        _seed(db)
        _fact(db, "m1")
        db.execute(
            "INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,attributes) "
            "VALUES ('sb','sb','measurement.correction','corsia','instrumented','2026-09-22','2026-09-22',"
            "'plan-review-rejected:r1','{\"label\":\"correction\",\"source_kind\":\"sent_back\"}')")
        db.commit()
    finally:
        db.close()

    rows = await _rows(monkeypatch)
    assert rows[("corrections_per_outcome", WEEK)]["value"] == pytest.approx(1 / 20)


@pytest.mark.asyncio
async def test_a_thin_period_serves_no_value(isolated_db, monkeypatch):
    from infra.database import get_db

    db = get_db()
    try:
        _seed(db, outcomes=5)
        _fact(db, "m1")
        db.commit()
    finally:
        db.close()

    row = (await _rows(monkeypatch))[("corrections_per_outcome", WEEK)]
    assert row["value"] is None and row["raw_value"] == pytest.approx(1 / 5) and row["n"] == 5


@pytest.mark.asyncio
async def test_a_document_comment_as_the_first_correction_starts_and_counts_the_series(isolated_db, monkeypatch):
    from infra.database import get_db

    db = get_db()
    try:
        _seed(db)
        db.execute(
            "INSERT INTO doc_comments (id,artifact_id,author,author_kind,body,created_at,updated_at) "
            "VALUES ('c1','a1','steve','user','x','2026-09-22T12:00:00Z','2026-09-22T12:00:00Z')")
        db.execute(
            "INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,"
            "agent_thread_id,attributes) VALUES ('f1','f1','measurement.correction','corsia','instrumented',"
            "'2026-10-01','2026-10-01','doc-comment:c1','root',?)",
            (json.dumps({"label": "correction", "asset_version": "v1", "classified_at": "2026-10-01T00:00:00Z"}),))
        db.commit()
    finally:
        db.close()

    rows = await _rows(monkeypatch)
    assert rows[("corrections_per_outcome", WEEK)]["value"] == pytest.approx(1 / 20)
