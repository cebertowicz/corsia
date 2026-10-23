"""Receipt-backed period offers preserve paging, canonical fingerprints, and report shape."""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from core import lesson_report as module
from core.lesson_report import ReportScope, read_report
from plans import closure
from tests.lesson_receipt_helpers import write_trial_receipt
from tests.test_lesson_report_inventory import source
from tests.test_lesson_report_scan_equivalence import assert_materialized_equals_the_scan, receipt_reference
from tests.test_lesson_review_history import db, review  # noqa: F401 - shared real-schema fixture

NOW = "2026-10-02T12:00:00Z"
SCOPE = ReportScope("user", ("owner",), "self", "2026-09-21", "2026-09-28")
# Strings that exercise every escape decision in the canonical receipt document.
AWKWARD = ["plain", 'quo"te', "back\\slash", "sl/ash", "ctl\x01\x1f\t\n\r", "del\x7f", "unié中\U0001f600",
           "sep  ", "", "tab\ttab"]


def _report(db, sources=(), **options):
    return read_report(db, scope=SCOPE, sources=list(sources), now=NOW, **options)


def _offset(cursor):
    return None if cursor is None else json.loads(base64.urlsafe_b64decode(cursor))["offset"]


def _shape(report):
    """The part of a review-bearing report that receipt offers determine."""
    shape = {"rows": [{key: row[key] for key in ("kind", "id", "version", "open_reviews", "indexed")}
                      for row in report.get("rows", [])],
             "next": _offset(report.get("next_cursor"))}
    detail = report.get("detail")
    if detail:
        shape["detail"] = {"indexed": detail["indexed"], "count": detail["reference_count"],
                           "references": [[ref["kind"], ref["value"] if ref["kind"] == "indexed" else None]
                                          for ref in detail["references"]]}
    return shape


def _receipt_offers(db, lesson_id, version):
    reference = receipt_reference(
        db, user_id="user", thread_ids=("owner",), from_ts=SCOPE.from_ts, to_ts=SCOPE.to_ts,
    )
    return [offer for offer in reference["period"]["offers"]
            if (offer["id"], offer["version"]) == (lesson_id, version)]


def _indexed_receipt_ids(report):
    return [reference["value"]["receipt_id"] for reference in report.get("detail", {}).get("references", [])
            if reference["kind"] == "indexed"]


def _seed(db, receipts=47):
    for index in range(receipts):
        created_at = f"2026-09-{22 + index % 5:02d}T00:00:{index % 60:02d}"
        write_trial_receipt(
            db, lesson_id="subject", behavioral_sha256="a" * 64, node_id=f"task-{index % 4}",
            checkpoint_generation_id=AWKWARD[index % len(AWKWARD)], created_at=created_at,
        )
        if index % 3:
            write_trial_receipt(
                db, lesson_id="other", behavioral_sha256="b" * 64, node_id=f"task-{index % 4}",
                checkpoint_generation_id=AWKWARD[(index + 1) % len(AWKWARD)], created_at=created_at,
            )


def _walk(db, project, **options):
    """Every page of a detail, projected, with the references seen."""
    pages, cursor = [], None
    while True:
        page = _report(db, cursor=cursor, **options)
        pages.append(project(page))
        cursor = page["next_cursor"]
        if cursor is None:
            return pages


def test_a_review_less_report_is_repeatable_and_keeps_one_snapshot_across_queries(db):
    _seed(db)
    current = source()
    options = dict(sources=[current])
    baseline = _report(db, **options)
    assert baseline == _report(db, **options)
    assert baseline["snapshot"] == _report(db, lesson_id="subject", **options)["snapshot"]
    subject = _walk(db, lambda page: page, lesson_id="subject", version="a" * 64, **options)
    other = _walk(db, lambda page: page, lesson_id="other", version="b" * 64, **options)
    assert [receipt_id for page in subject for receipt_id in _indexed_receipt_ids(page)] == [
        offer["receipt_id"] for offer in _receipt_offers(db, "subject", "a" * 64)
    ]
    assert [receipt_id for page in other for receipt_id in _indexed_receipt_ids(page)] == [
        offer["receipt_id"] for offer in _receipt_offers(db, "other", "b" * 64)
    ]
    assert _report(db, sources=[current], lesson_id=current["id"], version=current["version"])["detail"]["reference_count"] == 0


def test_review_bearing_list_preview_and_every_detail_page_keep_the_receipt_shape(db):
    for _ in range(7):
        closure.close_node(review(db), reason="No conclusion yet", db=db)
    review(db)
    review(db, subject="other", sha="b" * 64)
    _seed(db)
    expected_subject = _receipt_offers(db, "subject", "a" * 64)
    expected_other = _receipt_offers(db, "other", "b" * 64)
    listing = _report(db)
    preview = _report(db, preview=True)
    subject_row = next(row for row in listing["rows"] if (row["id"], row["version"]) == ("subject", "a" * 64))
    assert subject_row["indexed"] == {"receipts": len(expected_subject), "tasks": 4,
                                       "opportunity": None, "application": None, "efficacy": None,
                                       "reason": "task-start receipts prove delivery, not reading, use, or benefit"}
    assert len(preview["rows"]) <= len(listing["rows"])
    subject = _walk(db, lambda page: page, lesson_id="subject", version="a" * 64)
    refs = [ref for page in subject for ref in page["detail"]["references"]]
    assert [ref["kind"] for ref in refs].count("review") == 8
    assert [ref["value"]["receipt_id"] for ref in refs if ref["kind"] == "indexed"] == [
        offer["receipt_id"] for offer in expected_subject
    ]
    other = _walk(db, lambda page: page, lesson_id="other", version="b" * 64)
    assert [receipt_id for page in other for receipt_id in _indexed_receipt_ids(page)] == [
        offer["receipt_id"] for offer in expected_other
    ]


def _cursor(db, offset):
    """A cursor the report itself would mint for this snapshot and query, at an arbitrary offset."""
    page = _report(db, lesson_id="subject", version="a" * 64)
    query = {**SCOPE.__dict__, "lesson_id": "subject", "version": "a" * 64, "preview": False, "evidence": None}
    return module._cursor(page["snapshot"], query, offset)


def test_first_middle_last_zero_reference_and_past_end_pages(db):
    for _ in range(3):
        closure.close_node(review(db), reason="x", db=db)
    _seed(db, receipts=60)
    base = _report(db, lesson_id="subject", version="a" * 64)
    count = base["detail"]["reference_count"]
    assert count > 40
    expected = _receipt_offers(db, "subject", "a" * 64)
    review_count = 3
    # Pages begin at every offset class: inside reviews, at the review/offer seam, mid offers, the last page.
    for offset in (0, 1, 3, 19, 20, 21, 40, count - 1, count):
        page = _report(db, lesson_id="subject", version="a" * 64, cursor=_cursor(db, offset))
        assert len(page["detail"]["references"]) == max(min(20, count - offset), 0)
        receipt_start = max(offset - review_count, 0)
        receipt_end = max(min(offset + 20 - review_count, len(expected)), 0)
        assert _indexed_receipt_ids(page) == [offer["receipt_id"] for offer in expected[receipt_start:receipt_end]]
    assert _report(db, lesson_id="subject", version="z" * 64).get("detail") is None
    current = source()
    zero = _report(db, sources=[current], lesson_id=current["id"], version=current["version"])
    assert zero["detail"]["reference_count"] == 0 and zero["detail"]["references"] == []
    with pytest.raises(ValueError, match="invalid detail position"):
        _report(db, lesson_id="subject", version="a" * 64, cursor=_cursor(db, count + 1))


def test_capture_rows_keep_the_receipt_count_of_the_lesson_with_the_same_identity(db):
    _seed(db, receipts=25)
    review(db, kind="capture", subject="subject", sha="a" * 64)
    review(db, kind="lesson", subject="subject", sha="a" * 64)
    expected = _receipt_offers(db, "subject", "a" * 64)
    listing = _report(db)
    subject_rows = [row for row in listing["rows"] if (row["id"], row["version"]) == ("subject", "a" * 64)]
    assert {row["kind"] for row in subject_rows} == {"capture", "lesson"}
    assert all(row["indexed"]["receipts"] == len(expected) and row["indexed"]["tasks"] == 4 for row in subject_rows)
    detail = _report(db, lesson_id="subject", version="a" * 64)
    assert detail["detail"] is None
    assert detail["reason"] == "exact lesson/version not unique in this view"


def test_no_receipts_yields_empty_receipt_counts_and_a_stable_fingerprint(db):
    review(db)
    result = _report(db)
    assert result["rows"][0]["indexed"]["receipts"] == 0
    assert result["rows"][0]["indexed"]["tasks"] == 0
    assert result["snapshot"] == _report(db)["snapshot"]
    detail = _report(db, lesson_id="subject", version="a" * 64)["detail"]
    assert detail["reference_count"] == 1
    assert [reference["kind"] for reference in detail["references"]] == ["review"]


def test_sqlite_canonical_text_equals_the_receipt_row_reference(db):
    _seed(db, receipts=40)
    kwargs = dict(user_id="user", thread_ids=("owner",), from_ts=SCOPE.from_ts, to_ts=SCOPE.to_ts)
    reference = receipt_reference(db, **kwargs)
    assert any("sep  " in json.dumps(offer, ensure_ascii=False) for offer in reference["period"]["offers"]), \
        "awkward receipt values reached the compared text"
    from core.lesson_report_receipts import read_offer_evidence
    assert_materialized_equals_the_scan(read_offer_evidence(db, **kwargs), reference)


def test_chunk_boundaries_do_not_change_the_text(db, monkeypatch):
    from core import lesson_report_offers as offers_module
    from core.lesson_report import collect_report

    _seed(db, receipts=40)
    whole = collect_report(db, scope=SCOPE, sources=[])["period"]["offers"]
    monkeypatch.setattr(offers_module, "CHUNK_ROWS", 3)
    split = collect_report(db, scope=SCOPE, sources=[])["period"]["offers"]
    first, second = hashlib.sha256(), hashlib.sha256()
    whole.feed(first)
    split.feed(second)
    assert first.hexdigest() == second.hexdigest()
    assert len(split._chunks) > 1


def test_the_owner_list_is_probed_by_an_index_not_rescanned_per_receipt_row(db):
    """A flattened owner subquery is a bare json_each rescanned for every receipt row: minutes at scale."""
    from core import lesson_report_offers as offers_module

    params = (json.dumps(["owner"]), "user", "2026-09-21", "2026-09-28")
    plan = [row["detail"] for row in db.execute("EXPLAIN QUERY PLAN " + offers_module.PERIOD_SQL, params)]
    assert any(detail.startswith("SEARCH o ") for detail in plan), plan


def test_offers_outside_the_collected_detail_window_are_refused_not_invented(db):
    from core.lesson_report import collect_report

    _seed(db, receipts=40)
    offers = collect_report(db, scope=SCOPE, sources=[])["period"]["offers"]
    assert offers.total(("subject", "a" * 64)) > 20
    with pytest.raises(LookupError):
        offers.page(("subject", "a" * 64), 0, 5)
    assert offers.page(("subject", "a" * 64), 5, 5) == []


def test_prefilter_keeps_nan_and_infinity_metadata_python_resolves(db):
    from core.lesson_report_reads import _read_call
    from tests.test_lesson_report_scan_equivalence import PATH, _matching, _tool_row

    raw = '{"name": "read_file", "arguments": {"path": "%s"}, "turn_id": "turn", "latency": NaN, "cost": -Infinity}' % PATH
    _tool_row(db, "nan", raw)
    row = db.execute("SELECT id,thread_id,metadata FROM messages WHERE id='nan'").fetchone()
    assert _read_call(row)[0] is not None, "Python accepts NaN and Infinity metadata"
    assert _matching(db) == ["nan"], "the prefilter must keep what Python resolves"
