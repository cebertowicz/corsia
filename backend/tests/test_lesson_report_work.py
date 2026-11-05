"""The Learning report does each piece of evidence work once per request and keeps its exact result.

Counts operations (blob reads, manifest parses, offer visits, summaries) rather than wall time.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from core import lesson_report, lesson_report_receipts as _receipts
from core.lesson_files import _with_lesson, render_trial, with_review_ref  # noqa: F401
from core.lesson_report import ReportScope, read_report
from core.lesson_report_inventory import build_inventory
from core.lesson_report_reads import identify_recorded_reads
from core.lesson_report_receipts import read_offer_evidence, read_offers
from core.lesson_trials import Trial
from tests.test_lesson_report_receipts import call, db, manifest, offer_counts  # noqa: F401 - shared real-schema fixture
from tests.test_lesson_report_scan_equivalence import assert_offers_equal

NOW = "2026-10-02T12:00:00Z"
SCOPE = ReportScope("user", ("owner",), "self", "2026-09-21", "2026-09-28")
SHA = "a" * 64


def lesson_bytes(text: str = "Check before deciding.") -> bytes:
    raw = render_trial(
        lesson_id="6680cac5-3aa2-4a88-b491-352459567323", owner="owner", name="deciding",
        description="Use before deciding.", text=text, trial=Trial("hypothesis", "2026-10-01"),
        source_refs=[{"kind": "message", "reference": "correction"}],
    )
    return with_review_ref(raw, "review").encode()


class Reader:
    """A public blob reader double that counts exact (oid, sha) reads."""

    def __init__(self, blobs: dict[tuple[Any, Any], tuple[bytes | None, str | None]]):
        self.blobs = blobs
        self.calls: list[tuple[Any, Any]] = []

    def __call__(self, oid, sha):
        self.calls.append((oid, sha))
        try:
            return self.blobs.get((oid, sha), (None, "exact recorded file object unavailable"))
        except TypeError:  # an unhashable identifier is simply not a recorded object
            return None, "exact recorded file object unavailable"


def receipt(path: str, oid: Any = "1" * 40, sha: Any = SHA) -> dict[str, Any]:
    return {"kind": "guidance", "id": None, "version": None, "path": path, "raw_sha256": sha,
            "git_blob_oid": oid, "identity_reason": "workspace guidance history unavailable"}


# ---- 1. identify_recorded_reads: one reader call per exact recorded object ----

def test_duplicate_blob_is_read_once_but_parsed_for_every_receipt_and_path():
    good, plain = ("1" * 40, SHA), ("2" * 40, "b" * 64)
    blobs = {good: (lesson_bytes(), None), plain: (b"not a lesson", None),
             ("1" * 40, "c" * 64): (None, "recorded file identity mismatch")}
    reads = [receipt("/ws/a/SKILL.md"), receipt("/ws/b/SKILL.md"), receipt("/ws/c/SKILL.md", "2" * 40, "b" * 64),
             receipt("/ws/d/SKILL.md", "2" * 40, "b" * 64), receipt("/ws/e/SKILL.md", sha="c" * 64),
             receipt("/ws/f/SKILL.md", sha="c" * 64), receipt("/ws/g/SKILL.md", "3" * 40)]
    shared = Reader(blobs)
    result = identify_recorded_reads(reads, shared)
    assert len(shared.calls) == 4
    assert set(shared.calls) == {good, plain, ("1" * 40, "c" * 64), ("3" * 40, SHA)}
    expected = [identify_recorded_reads([read], Reader(blobs))[0] for read in reads]
    assert result == expected
    assert [read["recorded_source"]["path"] for read in result[:2]] == ["/ws/a/SKILL.md", "/ws/b/SKILL.md"]
    assert result[0]["id"] == result[1]["id"] and result[0]["version"] == result[1]["version"]
    assert result[2]["identity_reason"] == "recorded file is not a validated lesson or capture"
    assert result[4]["identity_reason"] == result[5]["identity_reason"] == "recorded file identity mismatch"
    assert result[6]["identity_reason"] == "exact recorded file object unavailable"


def test_unhashable_or_missing_identifiers_keep_the_same_refusal_without_new_errors():
    reader = Reader({})
    reads = [receipt("/ws/a", oid=["x"]), receipt("/ws/b", oid=["x"]), receipt("/ws/c", sha={"k": 1}),
             receipt("/ws/d", oid=None, sha=None)]
    result = identify_recorded_reads(reads, reader)
    assert [read["identity_reason"] for read in result] == ["exact recorded file object unavailable"] * 4
    assert identify_recorded_reads(reads, None)[0]["identity_reason"] == "workspace guidance history unavailable"
    assert identify_recorded_reads([], reader) == []


def test_reader_memo_is_request_local_and_a_read_error_is_not_cached():
    blobs = {("1" * 40, SHA): (lesson_bytes(), None)}
    first, second = Reader(blobs), Reader(blobs)
    identify_recorded_reads([receipt("/a")], first)
    identify_recorded_reads([receipt("/a")], second)
    assert len(first.calls) == len(second.calls) == 1

    class Flaky(Reader):
        def __call__(self, oid, sha):
            self.calls.append((oid, sha))
            if len(self.calls) == 1:
                raise RuntimeError("git failed")
            return lesson_bytes(), None

    flaky = Flaky({})
    with pytest.raises(RuntimeError):
        identify_recorded_reads([receipt("/a"), receipt("/b")], flaky)
    assert len(flaky.calls) == 1


# ---- 2. read_report summarizes only the requested slice ----

def seed_rows(db, first: int, last: int, offers_per_row: int = 5) -> None:
    """One lesson per row, each offered by ``offers_per_row`` calls over two tasks."""
    for row in range(first, last):
        manifest(db, f"m{row}", [{"id": f"lesson-{row:03}", "behavioral_sha256": f"{row:064x}"}])
        for n in range(offers_per_row):
            call(db, f"u-{row}-{n}", f"m{row}", task=f"t{n % 2}", attempt=f"a{n}")


def collected_report(db, rows: int = 45, reads_per_row: int = 2, detail=None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The collected evidence of seeded rows, with fabricated body reads, and the list-based offers oracle."""
    seed_rows(db, 0, rows)
    return collect_seeded(db, rows, reads_per_row, detail)


def collect_seeded(db, rows: int, reads_per_row: int = 2, detail=None):
    collected = lesson_report.collect_report(db, scope=SCOPE, sources=[], detail=detail)
    reads = [{"kind": "lesson", "id": f"lesson-{row:03}", "version": f"{row:064x}", "offered": False,
              "evidence_kind": "body read", "created_at": f"2026-09-2{n + 1}", "path": f"/p/{row}",
              "raw_sha256": SHA, "thread_id": "owner"}
             for row in range(rows) for n in range(reads_per_row)]
    collected["bodies"] = {"reads": reads, "unavailable_reads": {}, "coverage": "test"}
    oracle = read_offers(db, user_id="user", thread_ids=("owner",), from_ts=SCOPE.from_ts, to_ts=SCOPE.to_ts)
    return collected, oracle["offers"]


def report(collected, **options):
    return read_report(None, scope=SCOPE, sources=[], now=NOW, collected=collected, **options)


def oracle_summary(row, offers, reads):
    """The pre-change per-row summary, written independently of the module under test."""
    period = [offer for offer in offers if (offer["id"], offer["version"]) == (row["id"], row["version"])]
    return {key: row[key] for key in (
        "kind", "id", "version", "name", "activated", "state", "source_status", "owner_ids",
        "due", "review_after", "open_reviews", "lifecycle_reason",
        "review_due", "due_work", "review_due_unknown",
    )} | {"indexed": offer_counts(period),
          "source_scope": (row["current_source"] or {}).get("source_scope"),
          "source_scope_reason": "recorded source scope unavailable" if not row["current_source"] else None,
          "body_reads": sum((read["id"], read["version"]) == (row["id"], row["version"]) for read in reads),
          "delivered": 0,
          "next_call": f"lesson_report(lesson_id={json.dumps(row['id'])}, version={json.dumps(row['version'])})"}


def oracle_inventory(collected):
    reads = [read for read in collected["bodies"]["reads"]]
    return build_inventory(history=[], sources=[], retained=collected["retained"] + reads, now=NOW), [
        read for read in reads if SCOPE.from_ts <= read["created_at"] < SCOPE.to_ts]


@pytest.mark.parametrize("preview,limit", [(True, 5), (False, 20)])
def test_pages_summarize_only_the_slice_and_equal_the_full_pre_change_rows(db, monkeypatch, preview, limit):
    collected, offers = collected_report(db)
    inventory, period_reads = oracle_inventory(collected)
    expected = [oracle_summary(row, offers, period_reads) for row in inventory]
    summarized = []
    original = lesson_report._summary
    monkeypatch.setattr(lesson_report, "_summary", lambda row, *a, **k: summarized.append(row["id"]) or original(row, *a, **k))
    first = report(collected, preview=preview)
    assert first["rows"] == expected[:limit] and first["known_versions"] == len(expected) == 45
    assert len(summarized) == limit
    cursor = first["next_cursor"]
    pages = [first["rows"]]
    while cursor:
        page = report(collected, preview=preview, cursor=cursor)
        pages.append(page["rows"])
        cursor = page["next_cursor"]
    assert [row for page in pages for row in page] == expected
    assert len(summarized) == len(expected)
    with pytest.raises(ValueError, match="invalid report position"):
        report(collected, preview=preview, cursor=_cursor_at(first, collected, preview, 46))


def _cursor_at(first, collected, preview, offset):
    query = {**SCOPE.__dict__, "lesson_id": None, "version": None, "preview": preview, "evidence": None}
    return lesson_report._cursor(first["snapshot"], query, offset)


def test_detail_and_evidence_build_one_or_no_row_summaries(db, monkeypatch):
    seed_rows(db, 0, 45)
    row_id, row_version = "lesson-007", f"{7:064x}"
    detail_request = lesson_report.detail_request(row_id, row_version, None, None)
    collected, offers = collect_seeded(db, 45, detail=detail_request)
    inventory, period_reads = oracle_inventory(collected)
    row = next(row for row in inventory if (row["id"], row["version"]) == (row_id, row_version))
    calls = []
    original = lesson_report._summary
    monkeypatch.setattr(lesson_report, "_summary", lambda *a, **k: calls.append(1) or original(*a, **k))
    detail = report(collected, lesson_id=row["id"], version=row["version"])
    assert len(calls) == 1
    assert {key: value for key, value in detail["detail"].items()
            if key not in {"current_source", "references", "reference_count", "source_bytes",
                           "carrying_cost", "source_bytes_reason"}} == oracle_summary(row, offers, period_reads)
    assert detail["detail"]["reference_count"] == 5 + 2
    calls.clear()
    assert report(collected, evidence="unidentified_reads")["rows"] == []
    assert calls == []
    assert report(collected, lesson_id="missing")["detail"] is None and calls == []


def test_changed_input_gives_a_fresh_report_and_refuses_the_old_cursor(db):
    seed_rows(db, 0, 45)
    first = report(collect_seeded(db, 45)[0])
    seed_rows(db, 45, 46)
    changed = collect_seeded(db, 46)[0]
    with pytest.raises(ValueError, match="report changed"):
        report(changed, cursor=first["next_cursor"])
    assert report(changed)["known_versions"] == 46


# ---- 3. read_offer_evidence parses each distinct manifest once per call ----

def lessons(*ids):
    return [{"id": lesson, "behavioral_sha256": SHA} for lesson in ids]


def parses(monkeypatch):
    """One entry per manifest body handed to the manifest parser (calls without a manifest body never parse)."""
    seen = []
    real = _receipts._lessons
    monkeypatch.setattr(_receipts, "_lessons", lambda text: (seen.append(1) if text is not None else None) or real(text))
    return seen


def evidence(db, **window):
    return read_offer_evidence(db, user_id="user", thread_ids=("owner",),
                               from_ts=window.get("from_ts", "2026-09-21"), to_ts=window.get("to_ts", "2026-09-28"))


def test_a_shared_manifest_is_parsed_once_and_every_call_keeps_its_own_offer(db, monkeypatch):
    manifest(db, "m1", lessons("one", "two", "one"))
    manifest(db, "m2", lessons("three"))
    for index in range(12):
        call(db, f"u{index:02}", "m1" if index % 3 else "m2", task=None if index == 4 else "task", attempt=f"at{index}")
    seen = parses(monkeypatch)
    result = evidence(db)
    assert len(seen) == 2
    oracle = read_offers(db, user_id="user", thread_ids=("owner",), from_ts="2026-09-21", to_ts="2026-09-28")
    assert_offers_equal(result["period"]["offers"], oracle["offers"]) and len(oracle["offers"]) == 4 * 1 + 8 * 2
    assert result["period"]["calls"] == oracle["calls"] == 12
    assert [(o["id"], o["usage_id"]) for o in oracle["offers"] if o["usage_id"] == "u04"] == [
        ("one", "u04"), ("two", "u04")]
    assert {(o["id"], o["version"]) for o in result["retained"]} == {("one", SHA), ("two", SHA), ("three", SHA)}
    assert [(o["id"], o["usage_id"]) for o in result["retained"]] == [
        ("three", "u00"), ("one", "u01"), ("two", "u01")]


def test_missing_malformed_and_historical_manifests_keep_their_reasons_and_first_occurrence(db, monkeypatch):
    manifest(db, "ok", lessons("late"))
    db.execute("INSERT INTO context_manifests(id,harness,entries_json,entry_count,created_at) VALUES('bad','native','{}',0,'t')")
    db.execute("INSERT INTO context_manifests(id,harness,entries_json,entry_count,created_at) VALUES('plain','native','[1]',1,'t')")
    manifest(db, "old", lessons("early"))
    call(db, "history", "old")
    db.execute("UPDATE llm_usage SET created_at='2026-01-01' WHERE id='history'")
    for index, manifest_id in enumerate(["ok", "bad", "plain", "ok", None, "bad", "missing-row"]):
        call(db, f"p{index}", manifest_id)
    seen = parses(monkeypatch)
    result = evidence(db)
    parsed_once = len(seen)
    oracle = read_offers(db, user_id="user", thread_ids=("owner",), from_ts="2026-09-21", to_ts="2026-09-28")
    assert_offers_equal(result["period"]["offers"], oracle["offers"])
    assert result["period"]["unavailable_calls"] == oracle["unavailable_calls"] != {}
    assert result["period"]["calls"] == oracle["calls"] == 7
    assert {o["id"]: o["usage_id"] for o in result["retained"]} == {"early": "history", "late": "p0"}
    assert parsed_once == 4  # old, ok, bad and plain once each; null and dangling manifests never parse
