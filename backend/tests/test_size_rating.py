"""Estimated outcome size: import identity, newest-fact selection and the served series.

Synthetic data only. Every case answers one way the sized headline could show a
plausible but wrong number: a stale or out-of-scope rating admitted, an older
favorable rating resurfacing, a partial import, or an import date moving history.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from tests.plan_writer_authorizer import raw_plan_seed

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

OWNER = "fixture-owner"
USER = {"id": OWNER}
WEEK = "2026-09-21"
ACCEPTED_AT = "2026-09-23T12:00:00Z"
SHA = "a" * 64


def _thread(db, thread_id, project_id=None):
    db.execute(
        "INSERT INTO threads (id,user_id,title,project_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (thread_id, OWNER, thread_id, project_id, "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
    )


def _project(db, project_id):
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,'t','t')", (project_id, project_id))


def _nodes(db, owner, count, *, accepted_at=ACCEPTED_AT, evidence=False):
    plan_id = f"plan-{owner}"
    db.execute("INSERT OR IGNORE INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) "
               "VALUES (?,'thread',?,'active',1,'t','t')", (plan_id, owner))
    ids = []
    for number in range(count):
        node_id = f"{owner}-node-{number}"
        with raw_plan_seed(db):
            db.execute(
                "INSERT INTO plan_nodes (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,commitment_id,"
                "requirements_revision,revision,created_at,updated_at) VALUES (?,?,NULL,?,'task','Synthetic task','done',"
                "'accepted',?,1,1,'t','t')", (node_id, plan_id, number, node_id))
            db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,prior_state,new_state,payload_json,created_at) "
                       "VALUES (?,?,'test','accepted','active','accepted',?,?)",
                       (f"{node_id}-accepted", node_id, json.dumps({"evidence": "synthetic proof"}) if evidence else None, accepted_at))
        ids.append(node_id)
    return ids


def _presence_visit(db, thread_id, day="2026-09-22"):
    db.execute(
        "INSERT INTO presence_visits (user_id,surface,thread_id,entered_at,exited_at,seconds,sessions,exit_reason) "
        "VALUES ('steve','chat',?,?,?,720,1,'hidden')",
        (thread_id, f"{day}T10:00:00Z", f"{day}T10:12:00Z"),
    )


def _rows(db, node_ids, size_class="M", **overrides):
    from core.size_rating import current_snapshots

    snapshots = current_snapshots(db, node_ids)
    rows = []
    for node_id in node_ids:
        snap = snapshots[node_id]
        rows.append({
            "node_id": node_id, "plan_id": snap.plan_id, "node_revision": snap.node_revision,
            "evidence_event_id": snap.evidence_event_id, "source_snapshot_sha256": snap.sha256(),
            "rater_input_sha256": SHA, "method": "offline_proxy", "method_version": "1",
            "rubric_version": "1.3p", "rubric_sha256": SHA, "rater": "synthetic-rater",
            "rater_version": "2026-10-01", "basis": snap.basis, "size_class": size_class, **overrides,
        })
    return rows


def _import(db, rows, *, project="all", agent="none", apply=True):
    from core.size_rating_import import import_size_ratings

    return import_size_ratings(db, rows, user_id=OWNER, project=project, agent=agent, apply=apply)


def _table_state(db):
    return (db.execute("SELECT COUNT(*) FROM outcome_events").fetchone()[0],
            db.execute("SELECT COALESCE(SUM(n),0) FROM outcome_event_counters").fetchone()[0])


async def _size_rows(project="all", agent="none", grain="weeks"):
    from api.measurement import measurement_series

    result = await measurement_series("estimated_human_hours_per_active_hour", project=project, agent=agent, grain=grain, timezone_name="UTC", user=USER)
    return result["rows"]


def _week(rows, week=WEEK):
    return next(row for row in rows if row["period"] == week)


def _db():
    from infra.database import get_db

    return get_db()


def test_fact_allowlist_matches_the_size_owner():
    from core.size_rating import BASES, FACT_KEYS, SIZE_CLASSES, SIZE_EVENT
    from infra.outcome_event_attributes import ATTRIBUTE_ALLOWLISTS

    allowed = ATTRIBUTE_ALLOWLISTS[SIZE_EVENT]
    assert set(allowed) == set(FACT_KEYS)
    assert allowed["size_class"].values == frozenset(SIZE_CLASSES)
    assert allowed["basis"].values == frozenset(BASES)


@pytest.mark.asyncio
async def test_midpoints_coverage_and_identical_retry(isolated_db):
    db = _db()
    try:
        _thread(db, "root")
        nodes = _nodes(db, "root", 24)
        _presence_visit(db, "root")
        rows = _rows(db, nodes[:20], "M") + _rows(db, nodes[20:22], "unknown")
        report = _import(db, rows)
        db.commit()
        assert report["written"] == 22 and report["unchanged"] == 0
        observed = db.execute("SELECT MIN(observed_ts) FROM outcome_events").fetchone()[0]
        again = _import(db, rows)
        db.commit()
        assert again["written"] == 0 and again["unchanged"] == 22
        assert db.execute("SELECT COUNT(*),MIN(observed_ts) FROM outcome_events").fetchone()[:] == (22, observed)
    finally:
        db.close()
    row = _week(await _size_rows())
    size = row["size"]
    assert (size["outcomes"], size["sized"], size["unknown"], size["unsized"]) == (24, 20, 2, 2)
    # Four of 24 outcomes carry no usable size: the estimate is partial and says so.
    assert row["partial"] is True and size["rate_reason"] is None
    assert size["estimated_hours"] == pytest.approx(40.0)
    assert size["active_hours"] == pytest.approx(0.2)
    assert row["value"] == pytest.approx(200.0)
    assert row["n"] == 20 and row["proxy"] is True
    assert size["basis"] == {"delivered_evidence_proxy": 0, "accepted_scope_proxy": 20, "author_declared": 0}
    # This fresh fixture has no retained model/change mark before its first rating, so its
    # coverage starts at the instrument rather than inheriting a workspace-history boundary.
    assert (size["data_start"], size["comparability_before"]) == (WEEK, None)


@pytest.mark.asyncio
async def test_reimporting_an_earlier_or_retracted_rating_serves_it_again(isolated_db):
    from infra.outcome_events import retract_outcome_event

    async def served_hours():
        return _week(await _size_rows())["size"]["estimated_hours"]

    def apply(rows):
        report = _import(db, rows)
        db.commit()
        return report

    db = _db()
    try:
        _thread(db, "root")
        nodes = _nodes(db, "root", 20)
        _presence_visit(db, "root")
        rating_a, rating_b = _rows(db, nodes, "M"), _rows(db, nodes, "L")
        assert apply(rating_a)["written"] == 20
        assert apply(rating_b)["written"] == 20
        db.close()
        assert await served_hours() == pytest.approx(160.0)
        db = _db()
        # A dry run of the earlier rating reports the writes it would make and makes none.
        before = _table_state(db)
        preview = _import(db, rating_a, apply=False)
        assert (preview["would_write"], preview["written"], preview["unchanged"]) == (20, 0, 0)
        assert _table_state(db) == before
        # A -> B -> A: the re-imported earlier rating becomes the newest fact again.
        report = apply(rating_a)
        assert (report["written"], report["unchanged"]) == (20, 0)
        observed = db.execute("SELECT MAX(observed_ts) FROM outcome_events").fetchone()[0]
        repeat = apply(rating_a)
        assert (repeat["written"], repeat["unchanged"]) == (0, 20)
        assert db.execute("SELECT COUNT(*),MAX(observed_ts) FROM outcome_events").fetchone()[:] == (60, observed)
        db.close()
        assert await served_hours() == pytest.approx(40.0)
        db = _db()
        # Retract the newest ratings: the node is withdrawn, and older ratings never resurface.
        newest = [row[0] for row in db.execute(
            "SELECT dedupe_key FROM outcome_events ORDER BY observed_ts DESC LIMIT 20")]
        assert len(newest) == 20 and all(retract_outcome_event(db, key) for key in newest)
        db.commit()
        db.close()
        size = _week(await _size_rows())["size"]
        assert (size["sized"], size["retracted"], size["estimated_hours"]) == (0, 20, 0.0)
        db = _db()
        # Re-importing the retracted rating appends a new live fact; the tombstones stay.
        report = apply(rating_a)
        assert (report["written"], report["unchanged"]) == (20, 0)
        assert apply(rating_a)["written"] == 0
        assert db.execute("SELECT COUNT(*),COUNT(retracted_at) FROM outcome_events").fetchone()[:] == (80, 20)
    finally:
        db.close()
    size = _week(await _size_rows())["size"]
    assert (size["sized"], size["retracted"], size["estimated_hours"]) == (20, 0, pytest.approx(40.0))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "evidence", "plan", "snapshot", "out_of_scope", "duplicate", "lost"])
async def test_changed_identity_refuses_the_whole_import(isolated_db, change):
    from core.size_rating_import import SizeImportRefusal

    db = _db()
    try:
        _project(db, "a")
        _project(db, "b")
        _thread(db, "root", "a")
        _thread(db, "other", "b")
        nodes = _nodes(db, "root", 3)
        other = _nodes(db, "other", 1)
        rows = _rows(db, nodes)
        if change == "revision":
            db.execute("UPDATE plan_nodes SET revision=2 WHERE id=?", (nodes[2],))
        elif change == "evidence":
            db.execute("INSERT INTO plan_node_events (id,node_id,provenance,event,payload_json,created_at) "
                       "VALUES ('late','root-node-2','test','note',?, '2026-09-24T00:00:00Z')", (json.dumps({"evidence": "new"}),))
        elif change == "plan":
            rows[2]["plan_id"] = "plan-other"
        elif change == "snapshot":
            rows[2]["source_snapshot_sha256"] = "b" * 64
        elif change == "out_of_scope":
            rows += _rows(db, other)
        elif change == "duplicate":
            rows.append(dict(rows[0]))
        else:
            del rows[2]["rater_version"]
        before = _table_state(db)
        with pytest.raises(SizeImportRefusal):
            _import(db, rows, project="a")
        assert _table_state(db) == before == (0, 0)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_bad_final_row_leaves_zero_partial_writes(isolated_db):
    from core.size_rating_import import SizeImportRefusal

    db = _db()
    try:
        _thread(db, "root")
        rows = _rows(db, _nodes(db, "root", 5))
        rows[-1]["size_class"] = "XXXL"
        with pytest.raises(SizeImportRefusal) as refused:
            _import(db, rows)
        assert "row 5" in str(refused.value)
        assert _table_state(db) == (0, 0)
    finally:
        db.close()


def test_cli_dry_run_is_read_only_and_apply_commits(isolated_db, tmp_path, capsys):
    from infra.config import DB_PATH
    from size_rating_import import main

    db = _db()
    try:
        _thread(db, "root")
        rows = _rows(db, _nodes(db, "root", 3, evidence=True), "S")
        db.commit()
    finally:
        db.close()
    path = tmp_path / "ratings.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    args = ["--db", str(DB_PATH), "--user", OWNER, "--project", "all", "--agent", "none", "--input", str(path)]
    assert main(args) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["mode"] == "dry-run" and dry["would_write"] == 3 and dry["written"] == 0
    with sqlite3.connect(DB_PATH) as check:
        assert _table_state(check) == (0, 0)
    assert main([*args, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["mode"] == "apply" and applied["written"] == 3
    assert applied["basis"] == {"delivered_evidence_proxy": 3, "accepted_scope_proxy": 0}
    with sqlite3.connect(DB_PATH) as check:
        assert _table_state(check) == (3, 0)


def _force_fact(db, node_id, attributes, observed, *, key=None, retracted=False):
    db.execute(
        "INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,attributes,retracted_at) "
        "VALUES (?,?,'measurement.size_rating','corsia','imported',?,?,?,?,?)",
        (key or f"{node_id}-{observed}", key or f"{node_id}-{observed}", ACCEPTED_AT, observed, f"plan_node:{node_id}",
         '{"retracted":true}' if retracted else json.dumps(attributes), observed if retracted else None),
    )


@pytest.mark.asyncio
async def test_reimport_resolves_a_newest_tie_the_series_calls_a_conflict(isolated_db):
    db = _db()
    try:
        _thread(db, "root")
        nodes = _nodes(db, "root", 20)
        _presence_visit(db, "root")
        rows = _rows(db, nodes, "M")
        # Two live facts tied at the newest time with equal values in a different key
        # order: the served series compares raw text, so this is a conflict.
        for row in rows:
            _force_fact(db, row["node_id"], row, "2026-09-30T00:00:00+00:00", key=f"{row['node_id']}-a")
            _force_fact(db, row["node_id"], dict(reversed(list(row.items()))), "2026-09-30T00:00:00+00:00",
                        key=f"{row['node_id']}-b")
        db.commit()
        db.close()
        size = _week(await _size_rows())["size"]
        assert (size["sized"], size["conflict"]) == (0, 20)
        db = _db()
        preview = _import(db, rows, apply=False)
        assert (preview["would_write"], preview["unchanged"]) == (20, 0)
        report = _import(db, rows)
        db.commit()
        assert (report["written"], report["unchanged"]) == (20, 0)
        repeat = _import(db, rows)
        db.commit()
        assert (repeat["written"], repeat["unchanged"]) == (0, 20)
        assert db.execute("SELECT COUNT(*) FROM outcome_events").fetchone()[0] == 60
    finally:
        db.close()
    size = _week(await _size_rows())["size"]
    assert (size["sized"], size["conflict"], size["estimated_hours"]) == (20, 0, pytest.approx(40.0))


@pytest.mark.asyncio
async def test_newest_bad_fact_never_exposes_an_older_favorable_rating(isolated_db):
    db = _db()
    try:
        _thread(db, "root")
        nodes = _nodes(db, "root", 25)
        _presence_visit(db, "root")
        old = "2026-10-01T00:00:00+00:00"
        new = "2026-10-02T00:00:00+00:00"
        rows = {row["node_id"]: row for row in _rows(db, nodes, "XXL")}
        for row in rows.values():
            _force_fact(db, row["node_id"], row, old)
        _force_fact(db, nodes[0], {**rows[nodes[0]], "size_class": "unknown"}, new)
        _force_fact(db, nodes[1], rows[nodes[1]], new, retracted=True)
        _force_fact(db, nodes[2], {**rows[nodes[2]], "size_class": "bogus"}, new)
        _force_fact(db, nodes[3], {**rows[nodes[3]], "node_revision": 0}, new)
        _force_fact(db, nodes[4], {**rows[nodes[4]], "size_class": "XS"}, new, key="tie-a")
        _force_fact(db, nodes[4], {**rows[nodes[4]], "size_class": "S"}, new, key="tie-b")
        db.execute("UPDATE plan_nodes SET revision=5 WHERE id=?", (nodes[5],))
        db.commit()
    finally:
        db.close()
    size = _week(await _size_rows())["size"]
    assert (size["sized"], size["unknown"], size["retracted"], size["malformed"], size["stale"], size["conflict"]) == (19, 1, 1, 1, 2, 1)
    assert size["estimated_hours"] == pytest.approx(19 * 120)


@pytest.mark.asyncio
async def test_scopes_do_not_leak_and_import_time_does_not_move_history(isolated_db):
    db = _db()
    try:
        _project(db, "a")
        _project(db, "b")
        _thread(db, "a-root", "a")
        _thread(db, "b-root", "b")
        a_nodes = _nodes(db, "a-root", 20)
        b_nodes = _nodes(db, "b-root", 20)
        _presence_visit(db, "a-root")
        _presence_visit(db, "b-root")
        _import(db, _rows(db, a_nodes, "L"), project="a")
        _import(db, _rows(db, b_nodes, "XS"), project="b")
        db.commit()
    finally:
        db.close()
    a_rows = await _size_rows(project="a")
    b_rows = await _size_rows(project="b")
    assert _week(a_rows)["size"]["estimated_hours"] == pytest.approx(160)
    assert _week(b_rows)["size"]["estimated_hours"] == pytest.approx(2)
    assert _week(a_rows)["value"] == pytest.approx(800)
    # The import ran today; its observation week holds nothing and the latest
    # completed week is still served, with a null rate and zero coverage.
    latest = a_rows[-1]
    assert latest["period"] > WEEK and latest["value"] is None and latest["size"]["outcomes"] == 0
    assert all(row["size"]["sized"] == 0 for row in a_rows if row["period"] != WEEK)


@pytest.mark.asyncio
async def test_count_series_and_thresholds_are_unchanged_by_ratings(isolated_db):
    from api.measurement import measurement_series

    db = _db()
    try:
        _thread(db, "root")
        nodes = _nodes(db, "root", 30)
        _presence_visit(db, "root")
        db.commit()
        before = await measurement_series("outcomes,outcomes_per_active_hour", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
        _import(db, _rows(db, nodes[:10], "M"))
        db.commit()
    finally:
        db.close()
    after = await measurement_series("outcomes,outcomes_per_active_hour", project="all", agent="none", grain="weeks", timezone_name="UTC", user=USER)
    assert after["rows"] == before["rows"]
    row = _week(await _size_rows())
    assert row["n"] == 10 and row["value"] is None  # below the registry minimum of 20
    assert row["size"]["estimated_hours"] == pytest.approx(20)
    assert row["size"]["rate_reason"] == "below_minimum" and row["partial"] is True
    assert all(other["series"] != "outcomes" or other["partial"] is False for other in after["rows"])
    assert row["size"]["comparable_reason"] is None


@pytest.mark.asyncio
async def test_an_unrecorded_rater_version_does_not_break_comparability(isolated_db):
    """A missing rater version is absent provenance, not a change in rating method."""
    db = _db()
    try:
        _thread(db, "root")
        nodes = _nodes(db, "root", 40)
        _presence_visit(db, "root")
        _import(db, _rows(db, nodes[:20], "M", rater_version="unknown"))
        _import(db, _rows(db, nodes[20:], "M"))  # the same method with a recorded version
        db.commit()
    finally:
        db.close()
    row = _week(await _size_rows())
    assert row["comparable"] is True
    assert row["size"]["comparable_reason"] is None
    assert len(row["size"]["profiles"]) == 2


@pytest.mark.asyncio
async def test_a_change_of_rater_version_across_weeks_keeps_every_week_comparable(isolated_db):
    """Who rated is served as coverage: a range rated by two recorded versions still compares."""
    second = "2026-09-28"
    db = _db()
    try:
        _thread(db, "root")
        _thread(db, "later")
        first = _nodes(db, "root", 20)
        next_week = _nodes(db, "later", 20, accepted_at="2026-09-30T12:00:00Z")
        _presence_visit(db, "root")
        _import(db, _rows(db, first, "M", rater_version="unknown"))
        _import(db, _rows(db, next_week, "M", rater_version="2026-10-01"))
        db.commit()
    finally:
        db.close()
    rows = await _size_rows()
    assert [(_week(rows, week)["comparable"], _week(rows, week)["size"]["comparable_reason"]) for week in (WEEK, second)] == [(True, None)] * 2

    db = _db()
    try:
        _import(db, _rows(db, first, "M", rater_version="2026-10-05"), apply=True)
        db.commit()
    finally:
        db.close()
    rows = await _size_rows()
    for week in (WEEK, second):
        assert (_week(rows, week)["comparable"], _week(rows, week)["size"]["comparable_reason"]) == (True, None)
    assert len({(item["rater_version"]) for week in (WEEK, second) for item in _week(rows, week)["size"]["profiles"]}) == 2


@pytest.mark.asyncio
async def test_the_headline_window_pools_every_period_across_raters(isolated_db, monkeypatch):
    """A rater change breaks comparability before it, and the window the card names is pooled whole."""
    from datetime import datetime, timezone

    from api.measurement import measurement_series

    monkeypatch.setattr("core.measurement_scope._now", lambda: datetime(2026, 10, 9, 12, tzinfo=timezone.utc))
    second = "2026-09-28"
    db = _db()
    try:
        _thread(db, "root")
        _thread(db, "later")
        first = _nodes(db, "root", 20)
        next_week = _nodes(db, "later", 20, accepted_at="2026-09-30T12:00:00Z")
        _presence_visit(db, "root")
        db.execute(
            "INSERT INTO presence_visits (user_id,surface,thread_id,entered_at,exited_at,seconds,sessions,exit_reason) "
            "VALUES ('steve','chat','later','2026-09-30T10:00:00Z','2026-09-30T10:12:00Z',720,1,'hidden')"
        )
        _import(db, _rows(db, first, "M", rater_version="2026-10-01"))
        _import(db, _rows(db, next_week, "M", rater="other-rater", rater_version="2026-10-01"))
        db.commit()
    finally:
        db.close()
    served = await measurement_series("estimated_human_hours_per_active_hour", project="all", agent="none", grain="weeks",
                                      timezone_name="UTC", user=USER)
    # A changed rater is a model break at the oldest week its ratings count.
    assert [row["comparable"] for row in served["rows"] if row["period"] in (WEEK, second)] == [False, True]
    assert _week(served["rows"], WEEK)["size"]["comparable_reason"] == "judging_model_changed"
    latest = served["windows"]["estimated_human_hours_per_active_hour"]["latest"]
    # The rate divides by presence time, so the window starts at the first presence week.
    assert (latest["n"], latest["from"], latest["to"]) == (40, WEEK, "2026-10-04")
    assert latest["size"]["estimated_hours"] == pytest.approx(80.0)
    assert latest["size"]["active_hours"] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_the_rate_never_divides_work_from_before_presence_by_later_time(isolated_db, monkeypatch):
    """Sized hours from before the first presence keep their subtotal, but no row or window
    divides them by presence time recorded later."""
    from datetime import datetime, timezone

    from api.measurement import measurement_series

    monkeypatch.setattr("core.measurement_scope._now", lambda: datetime(2026, 10, 9, 12, tzinfo=timezone.utc))
    before = "2026-09-14"
    db = _db()
    try:
        _thread(db, "root")
        _thread(db, "earlier")
        _import(db, _rows(db, _nodes(db, "earlier", 20, accepted_at="2026-09-16T12:00:00Z"), "M"))
        _import(db, _rows(db, _nodes(db, "root", 20), "M"))
        _presence_visit(db, "root")
        db.commit()
    finally:
        db.close()
    served = await measurement_series("estimated_human_hours_per_active_hour", project="all", agent="none", grain="weeks",
                                      timezone_name="UTC", user=USER)
    early = next(row for row in served["rows"] if row["period"] == before)
    assert (early["value"], early["n"], early["size"]["rate_reason"]) == (None, 0, "before_data_start")
    assert early["size"]["estimated_hours"] == pytest.approx(40.0), "the subtotal is still served"
    assert early["size"]["data_start"] == WEEK
    latest = served["windows"]["estimated_human_hours_per_active_hour"]["latest"]
    assert (latest["n"], latest["from"]) == (20, WEEK)
    assert latest["size"]["estimated_hours"] == pytest.approx(40.0)
    assert latest["value"] == pytest.approx(40.0 / 0.2)


@pytest.mark.asyncio
async def test_a_week_rated_by_two_raters_stays_comparable_and_serves_both_profiles(isolated_db):
    db = _db()
    try:
        _thread(db, "root")
        nodes = _nodes(db, "root", 40)
        _presence_visit(db, "root")
        _import(db, _rows(db, nodes[:20], "M", rater_version="unknown"))
        _import(db, _rows(db, nodes[20:], "M", rater="other-rater"))
        db.commit()
    finally:
        db.close()
    row = _week(await _size_rows())
    assert row["comparable"] is True
    assert row["size"]["comparable_reason"] is None
    assert len(row["size"]["profiles"]) == 2


@pytest.mark.asyncio
async def test_two_recorded_rater_versions_in_one_week_stay_comparable(isolated_db):
    db = _db()
    try:
        _thread(db, "root")
        nodes = _nodes(db, "root", 40)
        _presence_visit(db, "root")
        _import(db, _rows(db, nodes[:20], "M", rater_version="2026-10-01"))
        _import(db, _rows(db, nodes[20:], "M", rater_version="2026-10-05"))
        db.commit()
    finally:
        db.close()
    row = _week(await _size_rows())
    assert row["comparable"] is True
    assert row["size"]["comparable_reason"] is None


@pytest.mark.parametrize("grain, early, first, accepted_early, accepted_first", [
    ("weeks", "2026-08-17", "2026-08-31", "2026-08-19T12:00:00Z", "2026-09-02T12:00:00Z"),
    ("days", "2026-08-30", "2026-08-31", "2026-08-30T12:00:00Z", "2026-08-31T12:00:00Z"),
])
@pytest.mark.asyncio
async def test_the_rated_series_starts_at_its_first_observed_period_and_keeps_that_periods_coverage(
        isolated_db, monkeypatch, grain, early, first, accepted_early, accepted_first):
    from datetime import datetime, timezone

    from api.measurement import measurement_series

    # Tuesday 2026-09-08: both grains' spines reach back over the size series' start (2026-08-31).
    monkeypatch.setattr("core.measurement_scope._now", lambda: datetime(2026, 9, 8, 12, tzinfo=timezone.utc))

    db = _db()
    try:
        _thread(db, "root")
        _thread(db, "late")
        early_nodes = _nodes(db, "root", 22, accepted_at=accepted_early)
        first_nodes = _nodes(db, "late", 22, accepted_at=accepted_first)
        db.executemany("INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES (?,'root','user','x','{}',?)",
                       [("e1", f"{early}T10:00:00Z"), ("e2", f"{early}T10:10:00Z"), ("f1", f"{first}T10:00:00Z"), ("f2", f"{first}T10:10:00Z")])
        _import(db, _rows(db, early_nodes[:21], "M"))
        _import(db, _rows(db, first_nodes[:21], "M"))
        db.commit()
    finally:
        db.close()
    # A companion series dated from Aug 10 widens the spine to the pre-start periods; the rated series must not follow it.
    ids = "estimated_human_hours_per_active_hour,took_recommendation_pct"
    result = await measurement_series(ids, project="all", agent="none", grain=grain, timezone_name="UTC", user=USER)
    rows = {(row["series"], row["period"]): row for row in result["rows"]}
    served = [week for series, week in rows if series == "estimated_human_hours_per_active_hour"]
    assert early in served and early == min(served), "the install-wide instrument start serves an early scope zero"
    assert ("took_recommendation_pct", early) not in rows, "a decision metric without retained source remains unmeasured"
    size = rows[("estimated_human_hours_per_active_hour", first)]["size"]
    assert (size["outcomes"], size["sized"], size["unsized"]) == (22, 21, 1)
    assert size["estimated_hours"] == pytest.approx(42)
    assert size["basis"]["accepted_scope_proxy"] == 21
    assert rows[("estimated_human_hours_per_active_hour", first)]["partial"] is True


@pytest.mark.asyncio
async def test_author_declared_size_counts_without_a_fact_and_never_replaces_one(isolated_db):
    db = _db()
    try:
        _thread(db, "root")
        nodes = _nodes(db, "root", 24)
        _presence_visit(db, "root")
        db.executemany(
            "UPDATE plan_nodes SET size=? WHERE id=?",
            [("M", node_id) for node_id in nodes[:21]] + [("unknown", nodes[22])],
        )
        _import(db, _rows(db, [nodes[20]], "L"))
        db.commit()
    finally:
        db.close()

    size = _week(await _size_rows())["size"]
    assert (size["outcomes"], size["sized"], size["unknown"], size["unsized"]) == (24, 21, 1, 2)
    assert size["estimated_hours"] == pytest.approx(48)
    assert size["basis"]["author_declared"] == 20
    assert size["basis"]["accepted_scope_proxy"] == 1
    # Declared and rater-imported sizes are both served as coverage; the week still compares.
    assert size["comparable_reason"] is None
    assert {profile["method"] for profile in size["profiles"]} == {"author_declared", "offline_proxy"}


@pytest.mark.asyncio
async def test_a_week_sized_only_by_declarations_names_its_method_and_stays_comparable(isolated_db):
    db = _db()
    try:
        _thread(db, "root")
        nodes = _nodes(db, "root", 20)
        _presence_visit(db, "root")
        db.executemany("UPDATE plan_nodes SET size='M' WHERE id=?", [(node_id,) for node_id in nodes])
        db.commit()
    finally:
        db.close()

    row = _week(await _size_rows())
    assert row["comparable"] is True
    assert [(item["method"], item["outcomes"]) for item in row["size"]["profiles"]] == [("author_declared", 20)]


@pytest.mark.asyncio
async def test_a_days_range_from_rated_to_declared_sizes_serves_every_day_as_comparable(isolated_db, monkeypatch):
    """New outcomes carry only the size their author declared; that must not break the range rated before them."""
    from datetime import datetime, timezone

    monkeypatch.setattr("core.measurement_scope._now", lambda: datetime(2026, 9, 25, 12, tzinfo=timezone.utc))
    db = _db()
    try:
        _thread(db, "rated")
        _thread(db, "declared")
        rated = _nodes(db, "rated", 20, accepted_at="2026-09-22T12:00:00Z")
        declared = _nodes(db, "declared", 20, accepted_at="2026-09-23T12:00:00Z")
        _presence_visit(db, "rated", "2026-09-22")
        _presence_visit(db, "declared", "2026-09-23")
        _import(db, _rows(db, rated, "M"))
        db.executemany("UPDATE plan_nodes SET size='M' WHERE id=?", [(node_id,) for node_id in declared])
        db.commit()
    finally:
        db.close()

    rows = await _size_rows(grain="days")
    with_figure = [row for row in rows if row["value"] is not None]
    assert {row["period"] for row in with_figure} == {"2026-09-22", "2026-09-23"}
    assert [(row["comparable"], row["size"]["comparable_reason"]) for row in with_figure] == [(True, None)] * 2
    assert {item["method"] for row in with_figure for item in row["size"]["profiles"]} == {"author_declared", "offline_proxy"}
