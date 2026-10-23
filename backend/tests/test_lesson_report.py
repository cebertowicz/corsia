"""One scoped report keeps history, bounded pages and explicit refresh semantics."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

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
    from core.lesson_delivery import TrialDelivery, _insert

    _insert(db, thread_id="owner", node_id="task", trial=TrialDelivery(
        "orphan", "a" * 64, Path("/trial/SKILL.md"), b"trial", "new", "- trial",
    ), variant="with", checkpoint_generation_id=None)
    db.execute("UPDATE guidance_loads SET created_at='2026-09-21'")
    result = report(db, scope=replace(SCOPE, thread_ids=("owner", "other")))
    assert result["coverage"]["missing_owner_scopes"] == ["other"]
    [row] = result["rows"]
    assert row["id"] == "orphan" and row["activated"] is True
    assert row["open_reviews"] == 0 and row["owner_ids"] == []
    assert row["source_status"] == "unavailable"
    assert result["coverage"]["full_activated_population"] is None
    detail = report(db, scope=replace(SCOPE, thread_ids=("owner", "other")), lesson_id="orphan", version="a" * 64)
    assert [reference["kind"] for reference in detail["detail"]["references"]] == ["indexed"]


def test_due_work_is_visible_when_selected_period_has_no_calls(db):
    current = source()
    result = report(db, sources=[current])
    assert result["rows"][0]["due"] is True
    assert result["rows"][0]["indexed"]["receipts"] == 0
    assert result["rows"][0]["indexed"]["efficacy"] is None


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
