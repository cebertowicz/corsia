"""Learning-status report producer: model-free facts and delivery seam."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    try:
        yield connection
    finally:
        connection.close()


def _event(db, *, event_name, ts, project_id=None, agent_thread_id="agent", subject_key="subject", attributes=None):
    db.execute(
        """INSERT INTO outcome_events
           (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,project_id,agent_thread_id,attributes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (f"{event_name}:{subject_key}", f"key:{event_name}:{subject_key}", event_name,
         "corsia", "instrumented", ts, ts, subject_key, project_id, agent_thread_id,
         json.dumps(attributes or {})),
    )


def test_disabled_verdict_source_is_not_reported_as_an_empty_experiment_list(db):
    from jobs.learning_report import build_report

    now = "2026-10-22T18:00:00+00:00"
    _event(db, event_name="measurement.correction", ts=now, project_id="proj", subject_key="message:a",
           attributes={"source_kind": "message", "label": "correction"})
    _event(db, event_name="measurement.correction", ts=now, project_id="proj", agent_thread_id="other", subject_key="comment:b",
           attributes={"source_kind": "doc_comment", "label": "correction"})
    _event(db, event_name="measurement.verdict.planned", ts=now, project_id="proj", subject_key="trial:a")
    db.commit()

    report = build_report(db, scope="proj", report_date="2026-10-22")

    assert report == {
        "scope": "proj", "report_date": "2026-10-22", "report_key": "proj:2026-10-22:daily", "wake": True,
        "items": {
            "corrections": {
                "doc_comment": {"count": 1, "refs": ["comment:b"]},
                "message": {"count": 1, "refs": ["message:a"]},
            },
            "lessons": {"recorded": {"count": 0, "refs": []}, "confirmed": None, "consolidated": None, "repeated": None},
            "experiments": {"started": None, "decided": None},
            "wins": None,
        },
    }


def test_daily_and_early_reports_validate_unchanged_with_sent_back(db):
    from events.learning_report import validated
    from jobs.learning_report import build_report

    _event(
        db, event_name="measurement.correction", ts="2026-10-22T18:00:00+00:00", project_id="proj",
        subject_key="sent-back:task", attributes={"source_kind": "sent_back", "label": "correction"},
    )
    db.commit()

    reports = [
        build_report(db, scope="proj", report_date="2026-10-22"),
        build_report(db, scope="proj", report_date="2026-10-22", report_kind="early-1"),
    ]

    assert [validated(report).as_contract() for report in reports] == reports


def test_learning_report_delivery_uses_the_real_sync_seam(db, monkeypatch):
    from events import learning_report as report_events
    from jobs import learning_report

    delivered = []

    def deliver(**kwargs):
        delivered.append(kwargs)
        return ["delivered"]

    monkeypatch.setattr(report_events, "deliver_measurement_report_sync", deliver)
    report = learning_report.build_report(db, scope="proj", report_date="2026-10-22")
    result = learning_report.deliver_learning_report_sync(report)
    assert result == ["delivered"]
    assert delivered == [{
        "scope": "proj", "event_type": "measurement.learning_report", "payload": report,
        "transaction_scope": "deliver_learning_report", "db": None,
    }]


def test_enabled_verdict_source_reports_started_trials_and_wakes(db, monkeypatch):
    from core import workflow_verdict_evaluator
    from jobs.learning_report import build_report

    monkeypatch.setattr(workflow_verdict_evaluator, "VERDICT_WRITES_ENABLED", True)
    _event(db, event_name="measurement.verdict.planned", ts="2026-10-22T18:00:00+00:00",
           project_id="proj", subject_key="trial:enabled")
    db.commit()

    report = build_report(db, scope="proj", report_date="2026-10-22")

    assert report["wake"] is True
    assert report["items"]["experiments"] == {
        "started": {"count": 1, "refs": ["trial:enabled"]},
        "decided": {"count": 0, "refs": []},
    }


def test_correction_refs_are_capped_without_losing_the_total_count(db):
    from jobs.learning_report import build_report

    for number in range(51):
        _event(
            db, event_name="measurement.correction", ts="2026-10-22T18:00:00+00:00", project_id="proj",
            subject_key=f"message:{number:02}", attributes={"source_kind": "message", "label": "correction"},
        )
    db.commit()

    report = build_report(db, scope="proj", report_date="2026-10-22")

    assert report["items"]["corrections"]["message"] == {
        "count": 51, "refs": [f"message:{number:02}" for number in range(50)],
    }


def test_early_threshold_includes_zero_days_and_uses_history_age_for_its_floor(db, monkeypatch):
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo

    from jobs import learning_report

    monkeypatch.setattr(learning_report, "install_timezone", lambda: ZoneInfo("UTC"))
    report_day = date(2026, 10, 22)
    _event(
        db, event_name="measurement.correction", ts="2026-09-12T12:00:00+00:00", project_id="established",
        subject_key="message:old", attributes={"source_kind": "message", "label": "correction"},
    )
    for offset in (2, 3, 4):
        _event(
            db, event_name="measurement.correction", ts=f"{report_day - timedelta(days=offset)}T12:00:00+00:00",
            project_id="established", subject_key=f"message:{offset}",
            attributes={"source_kind": "message", "label": "correction"},
        )
    _event(
        db, event_name="measurement.correction", ts="2026-10-12T12:00:00+00:00", project_id="new",
        subject_key="message:new", attributes={"source_kind": "message", "label": "correction"},
    )
    for scope in ("high", "quiet"):
        _event(
            db, event_name="measurement.correction", ts="2026-09-12T12:00:00+00:00", project_id=scope,
            subject_key=f"message:{scope}:old", attributes={"source_kind": "message", "label": "correction"},
        )
    for offset in range(1, 27):
        for number in range(8):
            _event(
                db, event_name="measurement.correction", ts=f"{report_day - timedelta(days=offset)}T12:00:00+00:00",
                project_id="high", subject_key=f"message:high:{offset}:{number}",
                attributes={"source_kind": "message", "label": "correction"},
            )
    db.commit()
    delivered = []
    monkeypatch.setattr(learning_report, "deliver_learning_report_sync", delivered.append)

    assert learning_report.early_threshold(db, scope="established", report_date=report_day.isoformat()) == learning_report.EARLY_FLOOR
    assert learning_report.early_threshold(db, scope="new", report_date=report_day.isoformat()) == learning_report.EARLY_FLOOR
    assert learning_report.early_threshold(db, scope="high", report_date=report_day.isoformat()) == 8
    assert not learning_report.maybe_emit_early(db, scope="quiet", report_date=report_day.isoformat())
    assert delivered == []


def test_sent_back_corrections_are_shown_separately_but_never_trigger_early_reports(db, monkeypatch):
    from datetime import date, timedelta
    from zoneinfo import ZoneInfo

    from jobs import learning_report

    monkeypatch.setattr(learning_report, "install_timezone", lambda: ZoneInfo("UTC"))
    report_day = date(2026, 10, 22)
    for offset in range(1, 27):
        timestamp = f"{report_day - timedelta(days=offset)}T12:00:00+00:00"
        for number in range(8):
            _event(
                db, event_name="measurement.correction", ts=timestamp, project_id="history",
                subject_key=f"message:{offset}:{number}",
                attributes={"source_kind": "message", "label": "correction"},
            )
        for number in range(9):
            _event(
                db, event_name="measurement.correction", ts=timestamp, project_id="history",
                subject_key=f"sent-back:{offset}:{number}",
                attributes={"source_kind": "sent_back", "label": "correction"},
            )
    for number in range(200):
        _event(
            db, event_name="measurement.correction", ts="2026-10-22T12:00:00+00:00", project_id="daily",
            subject_key=f"sent-back:today:{number:03}",
            attributes={"source_kind": "sent_back", "label": "correction"},
        )
    db.commit()
    delivered = []
    monkeypatch.setattr(learning_report, "deliver_learning_report_sync", delivered.append)

    report = learning_report.build_report(db, scope="daily", report_date=report_day.isoformat())

    assert learning_report.early_threshold(db, scope="history", report_date=report_day.isoformat()) == 8
    assert not learning_report.maybe_emit_early(db, scope="daily", report_date=report_day.isoformat())
    assert delivered == []
    assert report["wake"] is False
    assert report["items"]["corrections"] == {
        "sent_back": {"count": 200, "refs": [f"sent-back:today:{number:03}" for number in range(50)]},
    }


def test_early_report_uses_floor_then_cap_and_calls_only_the_adapter(db, monkeypatch):
    from jobs import learning_report

    delivered = []
    monkeypatch.setattr(learning_report, "deliver_learning_report_sync", delivered.append)
    today = "2026-10-22"
    for number in range(50):  # ten times the temporary floor threshold
        _event(db, event_name="measurement.correction", ts=f"{today}T18:00:00+00:00", project_id="proj",
               subject_key=f"message:{number}", attributes={"source_kind": "message", "label": "correction"})
        db.commit()
        learning_report.maybe_emit_early(db, scope="proj", report_date=today)

    assert {report["report_key"] for report in delivered} == {
        "proj:2026-10-22:early-1", "proj:2026-10-22:early-2",
    }
    assert delivered[-1]["wake"] is True


@pytest.mark.parametrize("zone_name", ["Europe/Berlin"])
def test_daily_reports_cover_two_completed_local_days_oldest_first(db, monkeypatch, zone_name):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from infra import database
    from jobs import learning_report

    zone = ZoneInfo(zone_name)
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('proj','Project','now','now')")
    _event(db, event_name="measurement.correction", ts="2026-10-21T21:30:00+00:00", project_id="proj",
           subject_key="message:older", attributes={"source_kind": "message", "label": "correction"})
    _event(db, event_name="measurement.correction", ts="2026-10-22T21:30:00+00:00", project_id="proj",
           subject_key="message:newer", attributes={"source_kind": "message", "label": "correction"})
    db.commit()
    delivered = []
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(learning_report, "deliver_learning_report_sync", delivered.append)
    monkeypatch.setattr(learning_report, "install_timezone", lambda: zone)

    delivered_keys = learning_report.emit_daily_reports(now=datetime(2026, 10, 23, 0, 5, tzinfo=zone))

    assert delivered_keys == ["proj:2026-10-21:daily", "proj:2026-10-22:daily"]
    assert [(report["scope"], report["report_date"]) for report in delivered] == [
        ("proj", "2026-10-21"), ("proj", "2026-10-22"),
    ]


def test_daily_reports_continue_after_one_scope_delivery_fails(db, monkeypatch):
    from datetime import datetime, timezone

    from infra import database
    from jobs import learning_report

    for scope in ("broken", "later"):
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)", (scope, scope, "now", "now"))
        _event(
            db, event_name="measurement.correction", ts="2026-10-22T12:00:00+00:00", project_id=scope,
            subject_key=f"message:{scope}", attributes={"source_kind": "message", "label": "correction"},
        )
    db.commit()
    attempted = []

    def deliver(report):
        attempted.append(report["report_key"])
        if report["scope"] == "broken":
            raise RuntimeError("designation failed")

    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(learning_report, "deliver_learning_report_sync", deliver)
    monkeypatch.setattr(learning_report, "install_timezone", lambda: timezone.utc)

    delivered = learning_report.emit_daily_reports(now=datetime(2026, 10, 23, tzinfo=timezone.utc))

    assert attempted == ["broken:2026-10-22:daily", "later:2026-10-22:daily"]
    assert delivered == ["later:2026-10-22:daily"]


def test_recorded_lessons_count_each_lesson_once_with_a_reference_per_version(db):
    from datetime import datetime, timezone

    from jobs import learning_report
    from plans import lesson_reviews

    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('proj','Project','t','t')")
    db.execute("INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at) "
               "VALUES ('owner','user','proj','test/model','t','t')")
    db.execute("INSERT INTO artifact_identities (id,integration,type,external_id,title,state,created_at,updated_at) "
               "VALUES ('art','workspace','skill','workspace://art','art','active','t','t')")
    review = {"artifact_id": "art", "title": "Review", "acceptance": "Decide.", "review_after": "2099-01-01T00:00:00+00:00"}
    today = learning_report.report_date_for(datetime.now(timezone.utc))

    def recorded():
        return learning_report.build_report(db, scope="proj", report_date=today)["items"]["lessons"]["recorded"]

    for lesson_id, sha in (("a", "1"), ("a", "2"), ("b", "1")):
        lesson_reviews.ensure_lesson_review(db, "owner", lesson_id=lesson_id, behavioral_sha256=sha, **review)
    assert (recorded()["count"], len(recorded()["refs"])) == (2, 3)

    lesson_reviews.ensure_capture_review(db, "owner", capture_id="a", capture_hash="1", **review)
    assert (recorded()["count"], len(recorded()["refs"])) == (2, 4)


def test_daily_report_is_passive_without_qualifying_facts(db):
    from jobs.learning_report import build_report

    report = build_report(db, scope="proj", report_date="2026-10-22")

    assert report["wake"] is False
    assert report["items"] == {
        "corrections": {},
        "lessons": {"recorded": {"count": 0, "refs": []}, "confirmed": None, "consolidated": None, "repeated": None},
        "experiments": {"started": None, "decided": None},
        "wins": None,
    }
