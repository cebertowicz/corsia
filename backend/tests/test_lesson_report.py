"""One scoped report keeps history, bounded pages and explicit refresh semantics."""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from core.lesson_report import ReportScope, _correction_inventory, _list_inventory, read_report
from plans import closure, public
from tests.test_lesson_report_inventory import source
from tests.test_lesson_review_history import db, review  # noqa: F401 - shared real-schema fixture

NOW = "2026-10-02T12:00:00Z"
SCOPE = ReportScope("user", ("owner",), "self", "2026-09-21", "2026-09-28")


def report(db, *, scope=SCOPE, sources=(), **options):
    return read_report(db, scope=scope, sources=list(sources), now=NOW, **options)


def test_over_200_reviews_survive_reader_pages_and_list_preview_is_bounded(db):
    for index in range(201):
        review(db, subject=f"lesson-{index:03}")
    result = report(db)
    assert result["known_versions"] == 201 and len(result["rows"]) == 20
    assert result["coverage"]["activation_unknown_versions"] == 201
    assert result["coverage"]["full_activated_population"] is None
    next_page = report(db, cursor=result["next_cursor"])
    assert len(next_page["rows"]) == 20
    assert not {row["id"] for row in result["rows"]} & {row["id"] for row in next_page["rows"]}
    assert len(report(db, preview=True)["rows"]) == 5
    with pytest.raises(ValueError, match="scope differs"):
        report(db, preview=True, cursor=result["next_cursor"])


def test_detail_can_page_all_reviews_of_the_same_version_without_duplicate_trials(db):
    for _ in range(21):
        node = review(db)
        closure.close_node(node, reason="No conclusion yet", db=db)
    first = report(db, lesson_id="subject", version="a" * 64)
    assert first["detail"]["reference_count"] == 21
    assert len(first["detail"]["references"]) == 20
    second = report(db, lesson_id="subject", version="a" * 64, cursor=first["next_cursor"])
    assert len(second["detail"]["references"]) == 1 and second["next_cursor"] is None
    assert report(db)["known_versions"] == 1


def test_changed_review_or_source_requires_refresh_instead_of_mixing_pages(db):
    for index in range(21):
        review(db, subject=str(index))
    first = report(db)
    review(db, subject="new")
    with pytest.raises(ValueError, match="report changed"):
        report(db, cursor=first["next_cursor"])
    first = report(db)
    with pytest.raises(ValueError, match="report changed"):
        report(db, sources=[source()], cursor=first["next_cursor"])


def test_foreign_owner_is_refused_before_any_history_adapter_call(db, monkeypatch):
    db.execute(
        "INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES('foreign','another','test/model','t','t')",
    )
    def no_history(*args, **kwargs):
        pytest.fail("foreign asserted scope reached the trusted history adapter")
    monkeypatch.setattr(public, "lesson_review_history", no_history)
    with pytest.raises(PermissionError):
        report(db, scope=replace(SCOPE, thread_ids=("owner", "foreign")))


def test_missing_owner_history_is_not_disclosed_but_authorized_exact_offer_survives(db):
    review(db, owner="other", subject="orphan")
    db.execute("DELETE FROM threads WHERE id='other'")
    entries = [{"source_id": "dynamic:skills-index", "lessons": [{"id": "orphan", "behavioral_sha256": "a" * 64}]}]
    db.execute(
        "INSERT INTO context_manifests(id,harness,entries_json,entry_count,created_at) VALUES('receipt','native',?,1,'t')",
        (json.dumps(entries),),
    )
    db.execute(
        "INSERT INTO llm_usage(id,thread_id,model,created_at,context_manifest_id) "
        "VALUES('call','owner','test/model','2026-09-21','receipt')",
    )
    result = report(db, scope=replace(SCOPE, thread_ids=("owner", "other")))
    assert result["coverage"]["missing_owner_scopes"] == ["other"]
    [row] = result["rows"]
    assert row["id"] == "orphan" and row["activated"] is True
    assert row["open_reviews"] == 0 and row["owner_ids"] == []
    assert row["source_status"] == "unavailable"
    assert result["coverage"]["full_activated_population"] is None
    detail = report(db, scope=replace(SCOPE, thread_ids=("owner", "other")), lesson_id="orphan", version="a" * 64)
    assert [reference["kind"] for reference in detail["detail"]["references"]] == ["indexed"]


def test_deliveries_are_scoped_separate_and_paged_with_snapshot_refresh(db):
    current = source()
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES('foreign','other','test/model','t','t')")

    def receipt(receipt_id, *, thread='owner', version=None, variant='with', created='2026-09-22'):
        db.execute(
            "INSERT INTO guidance_loads(id,thread_id,turn_id,path,raw_sha256,git_blob_oid,"
            "delivered_sha256,byte_count,partial,node_id,variant,checkpoint_generation_id,"
            "lesson_id,lesson_behavioral_sha256,created_at) VALUES(?,?,NULL,?,?,?,?,0,0,?,?,?,?,?,?)",
            (receipt_id, thread, current['path'], current['raw_sha256'], 'a' * 40, 'b' * 64,
             'task', variant, 'generation', current['id'], version or current['version'], created),
        )

    for index in range(21):
        receipt(f'receipt-{index:02}')
    receipt('other-version', version='f' * 64)
    receipt('outside-window', created='2026-09-28')
    receipt('omitted', variant=None)
    receipt('foreign', thread='foreign')
    entries = [{'source_id': 'dynamic:skills-index', 'lessons': [
        {'id': current['id'], 'behavioral_sha256': current['version']}]}]
    db.execute("INSERT INTO context_manifests(id,harness,entries_json,entry_count,created_at) "
               "VALUES('manifest','native',?,1,'t')", (json.dumps(entries),))
    db.execute("INSERT INTO llm_usage(id,thread_id,model,created_at,context_manifest_id) "
               "VALUES('call','owner','test/model','2026-09-22','manifest')")
    first = report(db, sources=[current], lesson_id=current['id'], version=current['version'])
    detail = first['detail']
    assert detail['delivered'] == 21 and detail['indexed']['calls'] == 1
    assert detail['reference_count'] == 22
    assert [ref['kind'] for ref in detail['references']] == ['indexed'] + ['delivered'] * 19
    assert detail['references'][1]['value'] == {
        'receipt_id': 'receipt-00', 'thread_id': 'owner', 'turn_id': None, 'task_id': 'task',
        'checkpoint_generation_id': 'generation', 'created_at': '2026-09-22',
        'lesson_id': current['id'], 'behavioral_sha256': current['version'],
        'path': current['path'], 'raw_sha256': current['raw_sha256'],
    }
    second = report(db, sources=[current], lesson_id=current['id'], version=current['version'],
                    cursor=first['next_cursor'])
    assert [ref['value']['receipt_id'] for ref in second['detail']['references']] == ['receipt-19', 'receipt-20']
    assert second['next_cursor'] is None
    assert report(db, sources=[current])['rows'][0]['delivered'] == 21
    receipt('receipt-21')
    with pytest.raises(ValueError, match='report changed'):
        report(db, sources=[current], lesson_id=current['id'], version=current['version'],
               cursor=first['next_cursor'])


def test_due_work_is_visible_when_selected_period_has_no_calls(db):
    current = source()
    result = report(db, sources=[current])
    assert result["rows"][0]["due"] is True
    assert result["rows"][0]["indexed"]["calls"] == 0
    assert result["rows"][0]["indexed"]["efficacy"] is None


def test_report_keeps_save_observations_separate_from_lesson_gate_counts(db):
    from infra.outcome_events import record_outcome_event

    for reason in ("rewording", "stewardship"):
        record_outcome_event(
            db,
            dedupe_key=f"lesson-gate-observed:{reason}",
            event_name="measurement.lesson_gate",
            source="corsia",
            provenance="instrumented",
            ts="2026-09-22T12:00:00+00:00",
            agent_thread_id="owner",
            attributes={"result": "observed", "reason": reason, "verdict": "save"},
        )

    gate = report(db)["coverage"]["lesson_gate"]

    assert gate["observed"] == {"rewording": 1, "stewardship": 1}
    assert {key: gate[key] for key in ("accepted", "refused", "widens")} == {
        "accepted": 0, "refused": 0, "widens": 0,
    }


def test_report_hides_only_old_watches_without_a_recent_correction_anchor():
    rows = _correction_inventory([
        {"id": "old", "version": "v", "state": "watch", "originating_subjects": [],
         "reviews": [{"opened_at": "2026-08-23T00:00:00Z"}]},
        {"id": "recent", "version": "v", "state": "watch", "originating_subjects": [],
         "reviews": [{"opened_at": "2026-09-22T00:00:00Z"}]},
        {"id": "recurred", "version": "v", "state": "watch", "originating_subjects": ["message:new"],
         "reviews": [{"opened_at": "2026-08-23T00:00:00Z"}]},
    ], {"message:new": {"id": "fact", "ts": "2026-09-20T00:00:00+00:00"}})

    assert [row["id"] for row in rows] == ["old", "recent", "recurred"]
    assert [row["id"] for row in _list_inventory(rows, "2026-10-02T12:00:00Z")] == ["recent", "recurred"]
    assert rows[2]["correction_status"] == "linked"
    assert rows[2]["recurrence_count"] == 1


def _plan(subject: str, state_mdc) -> str:
    from core.workflow_verdict_types import FrozenPlan
    return FrozenPlan(subject, "v", None, (), (), 20, (), 1, mdc=state_mdc).encoded()


def _record_plan(db, subject: str, mdc) -> None:
    db.execute("INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,"
               "attributes) VALUES (?,?,'measurement.verdict.planned','corsia','instrumented','2026-10-01','2026-10-01',?,?)",
               (subject, subject, subject, json.dumps({"plan": _plan(subject, mdc)})))


def test_trial_detail_shows_the_recorded_plans_confidence_and_mdc_with_unknown_as_null(db):
    trial = source()
    subject = f"lesson:{trial['id']}@{trial['version']}"
    assert "verdict_plan" not in report(db, sources=[trial], lesson_id=trial["id"], version=trial["version"])["detail"]
    _record_plan(db, subject, {"escapes": (0.5, None, 0.2)})
    plan = report(db, sources=[trial], lesson_id=trial["id"], version=trial["version"])["detail"]["verdict_plan"]
    assert plan["mdc"] == {"escapes": [0.5, None, 0.2]}
    assert plan["confidence"] == 0.8
    assert [round(value, 4) for value in plan["look_confidence"]] == [0.9931, 0.9438, 0.8231]
    retired = source(state="retired")
    assert "verdict_plan" not in report(db, sources=[retired], lesson_id=retired["id"], version=retired["version"])["detail"]


def test_the_trial_detail_reads_only_its_lessons_subjects_through_the_subject_index(db, monkeypatch):
    from core import workflow_verdict_read

    trial = source()
    _record_plan(db, f"lesson:{trial['id']}@{trial['version']}", {})
    _record_plan(db, f"lesson:{trial['id']}x@{trial['version']}", {})  # a neighbouring lesson id
    seen: list[str] = []
    real = workflow_verdict_read._recorded

    def spy(conn, index, where, params):
        seen.append(index)
        plan = conn.execute(f"EXPLAIN QUERY PLAN SELECT 1 FROM outcome_events{index} WHERE event_name IN "
                            f"(:planned, :reached) AND retracted_at IS NULL AND subject_key IS NOT NULL{where}",
                            {"planned": "p", "reached": "r", **params}).fetchall()
        assert any("idx_outcome_events_subject" in str(tuple(row)) for row in plan)
        return real(conn, index, where, params)

    monkeypatch.setattr(workflow_verdict_read, "_recorded", spy)
    assert set(workflow_verdict_read.recorded_lesson_plans(db, trial["id"])) == {trial["version"]}
    assert len(seen) == 1  # one query per detail read
