"""Period offers kept in SQLite render what origin/dev's in-memory list rendered, including paging and fingerprint.

Origin/dev is the oracle, frozen as digests. A review-less fixture is deterministic, so its historical report
(snapshot fingerprint and cursors included) is frozen after projecting away delivery evidence. Reviews mint ids and timestamps, so a review-bearing
fixture freezes only the offer-derived shape: indexed counts, reference kinds and offers, reference count and
the next cursor's offset. Regenerate by running ``_digest`` over the same helpers on an origin/dev checkout.
"""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from core import lesson_report as module
from core.lesson_report import ReportScope, read_report
from plans import closure
from tests.test_lesson_report_inventory import source
from tests.test_lesson_review_history import db, review  # noqa: F401 - shared real-schema fixture

NOW = "2026-10-02T12:00:00Z"
SCOPE = ReportScope("user", ("owner",), "self", "2026-09-21", "2026-09-28")
# Strings that exercise every escape decision between SQLite's and Python's canonical JSON.
AWKWARD = ["plain", 'quo"te', "back\\slash", "sl/ash", "ctl\x01\x1f\t\n\r", "del\x7f", "unié中\U0001f600",
           "sep  ", "", "tab\ttab"]
# Digests of what origin/dev (merge-base 8468cd650) produced for each label below.
FROZEN: dict[str, str] = {
    "full-list": "8bb8fd673086eb9c18c8d1564381be93611038524cdc01c1e17bdf0543c8d716",
    "full-preview": "8bb8fd673086eb9c18c8d1564381be93611038524cdc01c1e17bdf0543c8d716",
    "full-subject-a": "3cd396ea5cfe47c2b8fca93ac8ff852f6bf10f80d2e7f46023fc05d50c5af4e2",
    "full-other-b": "24c178a94c459b728037696364913da5bb9a0f96ef4e2295469466c69871192e",
    "full-subject": "f68058f51912dcc769c6d44a0e9a2728644fbd49d3ff34726c4f7255a66503da",
    "full-current": "8bd6cd065a86d4b3d074f9157823d82df85261d3f35216a6b5ac13f90a3fa104",
    "shape-list": "292d3f92d7ba125c0cd9e9dde2275d91801e657dfd8c7806ddca5286c955d7ac",
    "shape-preview": "292d3f92d7ba125c0cd9e9dde2275d91801e657dfd8c7806ddca5286c955d7ac",
    "shape-subject-a": "9d05788418c43dfb69e13183c14c2b30bdd6a1f904c857bb6653ce1ce95eb567",
    "shape-other-b": "20c4307c2f4f2b04856cb1dc53cc9f486641f3cea529d3ff3cd300e7e2dd862e",
    "shape-subject": "9d05788418c43dfb69e13183c14c2b30bdd6a1f904c857bb6653ce1ce95eb567",
    "pages-by-offset": "ca2630fae1382c6995b372a767c5856b72aa762f0f251e8d8e337557eedc4ff9",
    "capture-list": "4cc35ed791e7f8a309f418630103a5dd24ca57ff2b655fb26448b8b5055cc330",
    "capture-detail": "6e4aebc37bb5d4195874c471c2489d79da4e08878b1792eeb2e99cf828264ce6",
    "no-usage-list": "144ccfd244ffaadd7f7a5bd6df6aef177c57020861b63d7162ff25b8fe7f197f",
    "no-usage-detail": "589c49084425e90735c7a7dd8ce6df0d6991f65819135bc0e5fb867a904eb3b8",
}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _expect(label, value):
    assert FROZEN[label] == _digest(value), f"{label} differs from origin/dev"


def _manifest(db, identity, lessons):
    db.execute("INSERT INTO context_manifests(id,harness,entries_json,entry_count,created_at) VALUES(?,'native',?,1,'t')",
               (identity, json.dumps([{"source_id": "dynamic:skills-index", "lessons": lessons}])))


def _lesson(identity, version="a"):
    return {"id": identity, "behavioral_sha256": version * 64}


def _seed(db, calls=47):
    _manifest(db, "both", [_lesson("subject"), _lesson("other", "b")])
    _manifest(db, "one", [_lesson("subject")])
    for index in range(calls):
        db.execute(
            "INSERT INTO llm_usage(id,thread_id,turn_id,model,context_manifest_id,created_at,"
            "plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"call-{index:03d}", "owner", f"turn-{index}", AWKWARD[index % len(AWKWARD)],
             "both" if index % 3 else "one", f"2026-09-{22 + index % 5:02d}T00:00:{index % 60:02d}",
             *(("plan", f"task-{index % 4}", f"att-{index % 6}", "c", "execution") if index % 2 else (None,) * 5)),
        )


def _report(db, sources=(), **options):
    return read_report(db, scope=SCOPE, sources=list(sources), now=NOW, **options)


def _offset(cursor):
    return None if cursor is None else json.loads(base64.urlsafe_b64decode(cursor))["offset"]


def _shape(report):
    """The part of a review-bearing report that offers determine."""
    shape = {"rows": [{key: row[key] for key in ("kind", "id", "version", "open_reviews", "indexed")}
                      for row in report.get("rows", [])],
             "next": _offset(report.get("next_cursor"))}
    detail = report.get("detail")
    if detail:
        shape["detail"] = {"indexed": detail["indexed"], "count": detail["reference_count"],
                           "references": [[ref["kind"], ref["value"] if ref["kind"] == "indexed" else None]
                                          for ref in detail["references"]]}
    return shape


def _walk(db, project, **options):
    """Every page of a detail, projected, with the references seen."""
    pages, cursor = [], None
    while True:
        page = _report(db, cursor=cursor, **options)
        pages.append(project(page))
        cursor = page["next_cursor"]
        if cursor is None:
            return pages


def test_a_review_less_report_equals_origin_dev_byte_for_byte(db, monkeypatch):
    _seed(db)
    current = source()
    options = dict(sources=[current])
    new_report = _report(db, **options)
    assert all(row["delivered"] == 0 for row in new_report["rows"])
    assert new_report["snapshot"] == _report(db, lesson_id="subject", **options)["snapshot"]

    # Reconstruct the historical fingerprint, including the snapshot embedded in cursors;
    # the new fingerprint's delivery sensitivity is tested with real receipts separately.
    fingerprint = module.snapshot_fingerprint
    def historical_fingerprint(snapshot, canonical):
        assert snapshot["deliveries"] == []
        return fingerprint({key: value for key, value in snapshot.items() if key != "deliveries"}, canonical)
    monkeypatch.setattr(module, "snapshot_fingerprint", historical_fingerprint)

    def historical(page):
        for row in page.get("rows", []):
            assert row.pop("delivered") == 0
        if page.get("detail"):
            assert page["detail"].pop("delivered") == 0
        return page

    old_report = historical(_report(db, **options))
    assert new_report["snapshot"] != old_report["snapshot"]
    _expect("full-list", old_report)
    _expect("full-preview", historical(_report(db, preview=True, **options)))
    _expect("full-subject-a", _walk(db, historical, lesson_id="subject", version="a" * 64, **options))
    _expect("full-other-b", _walk(db, historical, lesson_id="other", version="b" * 64, **options))
    _expect("full-subject", _walk(db, historical, lesson_id="subject", **options))
    _expect("full-current", _walk(db, historical, lesson_id=current["id"], version=current["version"], **options))
    assert _report(db)["snapshot"] == _report(db, lesson_id="subject")["snapshot"]


def test_review_bearing_list_preview_and_every_detail_page_keep_the_offer_shape(db):
    for _ in range(7):
        closure.close_node(review(db), reason="No conclusion yet", db=db)
    review(db)
    review(db, subject="other", sha="b" * 64)
    _seed(db)
    _expect("shape-list", _shape(_report(db)))
    _expect("shape-preview", _shape(_report(db, preview=True)))
    subject = _walk(db, _shape, lesson_id="subject", version="a" * 64)
    refs = [ref for page in subject for ref in page["detail"]["references"]]
    assert [kind for kind, _value in refs].count("review") == 8 and len(refs) > 40
    _expect("shape-subject-a", subject)
    _expect("shape-other-b", _walk(db, _shape, lesson_id="other", version="b" * 64))
    _expect("shape-subject", _walk(db, _shape, lesson_id="subject"))


def _cursor(db, offset):
    """A cursor the report itself would mint for this snapshot and query, at an arbitrary offset."""
    page = _report(db, lesson_id="subject", version="a" * 64)
    query = {**SCOPE.__dict__, "lesson_id": "subject", "version": "a" * 64, "preview": False, "evidence": None}
    return module._cursor(page["snapshot"], query, offset)


def test_first_middle_last_zero_reference_and_past_end_pages(db):
    for _ in range(3):
        closure.close_node(review(db), reason="x", db=db)
    _seed(db, calls=60)
    base = _report(db, lesson_id="subject", version="a" * 64)
    count = base["detail"]["reference_count"]
    assert count > 40
    shapes = {}
    # Pages begin at every offset class: inside reviews, at the review/offer seam, mid offers, the last page.
    for offset in (0, 1, 3, 19, 20, 21, 40, count - 1, count):
        page = _report(db, lesson_id="subject", version="a" * 64, cursor=_cursor(db, offset))
        assert len(page["detail"]["references"]) == max(min(20, count - offset), 0)
        shapes[offset] = _shape(page)
    _expect("pages-by-offset", shapes)
    assert _report(db, lesson_id="subject", version="z" * 64).get("detail") is None
    current = source()
    zero = _report(db, sources=[current], lesson_id=current["id"], version=current["version"])
    assert zero["detail"]["reference_count"] == 0 and zero["detail"]["references"] == []
    with pytest.raises(ValueError, match="invalid detail position"):
        _report(db, lesson_id="subject", version="a" * 64, cursor=_cursor(db, count + 1))


def test_capture_rows_keep_the_count_of_the_lesson_with_the_same_identity(db):
    _seed(db, calls=25)
    review(db, kind="capture", subject="subject", sha="a" * 64)
    review(db, kind="lesson", subject="subject", sha="a" * 64)
    _expect("capture-list", _shape(_report(db)))
    # Same id and version under two kinds is ambiguous without a kind; origin/dev agrees on that too.
    _expect("capture-detail", _shape(_report(db, lesson_id="subject", version="a" * 64)))


def test_no_usage_yields_empty_offers_and_the_same_fingerprint(db):
    review(db)
    result = _report(db)
    assert result["rows"][0]["indexed"]["calls"] == 0
    _expect("no-usage-list", _shape(result))
    _expect("no-usage-detail", _shape(_report(db, lesson_id="subject", version="a" * 64)))


def test_sqlite_canonical_text_equals_python_canonical_json(db):
    from tests.test_lesson_report_scan_equivalence import assert_materialized_equals_the_scan, old_read_offer_evidence
    from core.lesson_report_receipts import read_offer_evidence

    _seed(db, calls=40)
    kwargs = dict(user_id="user", thread_ids=("owner",), from_ts=SCOPE.from_ts, to_ts=SCOPE.to_ts)
    old = old_read_offer_evidence(db, **kwargs)
    assert any("sep  " in json.dumps(offer, ensure_ascii=False) for offer in old["period"]["offers"]), \
        "awkward values reached the compared text"
    assert_materialized_equals_the_scan(read_offer_evidence(db, **kwargs), old)


def test_chunk_boundaries_do_not_change_the_text(db, monkeypatch):
    from core import lesson_report_offers as offers_module
    from core.lesson_report import collect_report

    _seed(db, calls=40)
    whole = collect_report(db, scope=SCOPE, sources=[])["period"]["offers"]
    monkeypatch.setattr(offers_module, "CHUNK_ROWS", 3)
    split = collect_report(db, scope=SCOPE, sources=[])["period"]["offers"]
    first, second = hashlib.sha256(), hashlib.sha256()
    whole.feed(first)
    split.feed(second)
    assert first.hexdigest() == second.hexdigest()
    assert len(split._chunks) > 1


def test_the_owner_list_is_probed_by_an_index_not_rescanned_per_candidate_row(db):
    """A flattened owner subquery is a bare json_each rescanned for every candidate row: minutes at scale."""
    from core import lesson_report_offers as offers_module

    params = (json.dumps([["both", 0, "subject", "a" * 64]]), json.dumps(["owner"]), "user", "2026-09-21", "2026-09-28")
    plan = [row["detail"] for row in db.execute("EXPLAIN QUERY PLAN " + offers_module.PERIOD_SQL, params)]
    assert any(detail.startswith("SEARCH o ") for detail in plan), plan


def test_offers_outside_the_collected_detail_window_are_refused_not_invented(db):
    from core.lesson_report import collect_report

    _seed(db, calls=40)
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
