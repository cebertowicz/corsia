"""Lesson-report scans return fewer SQLite rows but exactly the rows the row-by-row scans did."""
from __future__ import annotations

import hashlib
import json
from collections import Counter

import pytest

from core.lesson_report_receipts import _lessons, read_offer_evidence
from core.lesson_report_reads import _candidate_groups, _read_call
from core.lesson_report_sql import json_row_chunks, read_file_metadata_predicate
from tests.test_lesson_report_receipts import db  # noqa: F401  (fixture)

PATH = "/authorized/skills/deciding/SKILL.md"


def _tool_row(db, identity, metadata, *, thread="owner", seq=None):
    raw = metadata if isinstance(metadata, (str, type(None))) else json.dumps(metadata)
    db.execute(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) VALUES(?,?,'tool','body',?,'t',?)",
        (identity, thread, raw, seq if seq is not None else db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE thread_id=?", (thread,)).fetchone()[0]),
    )


def _call(name, **arguments):
    return {"name": name, "arguments": arguments, "turn_id": "turn"}


def _dispatch(target, dispatcher="tool_call"):
    return {"name": dispatcher, "arguments": {"name": target, "arguments": {"path": PATH}}, "turn_id": "turn"}


READS = {
    "bare": _call("read_file", path=PATH),
    "mcp prefix": _call("mcp__corsia__read_file", path=PATH),
    "alias": _call("Read", path=PATH),
    "mcp prefixed alias": _call("mcp__corsia__Read", path=PATH),
    "dispatch bare": _dispatch("read_file"),
    "dispatch mcp": _dispatch("mcp__corsia__read_file"),
    "dispatch alias": _dispatch("Read"),
    "dispatch mcp alias": _dispatch("mcp__corsia__Read"),
    "mcp dispatcher": _dispatch("read_file", "mcp__corsia__tool_call"),
}
NOT_READS = {
    "bash": _call("bash", command="cat " + PATH),
    "write alias": _call("Write", path=PATH),
    "dispatch bash": _dispatch("bash"),
    "decoy path": _call("bash", command="read_file Read"),
    "decoy nested name": {"name": "bash", "arguments": {"name": "read_file"}, "turn_id": "turn"},
    "malformed dispatch": {"name": "tool_call", "arguments": {"name": "read_file"}, "turn_id": "turn"},
    "no name": {"turn_id": "turn"},
    "array": ["read_file"],
}
SPELLED = {
    # Raw JSON text a writer other than json.dumps may have stored: escapes decode to the same names.
    "escaped underscore": '{"name": "read\\u005ffile", "arguments": {"path": "' + PATH + '"}, "turn_id": "turn"}',
    "escaped alias": '{"name": "\\u0052ead", "arguments": {"path": "' + PATH + '"}, "turn_id": "turn"}',
    "escaped prefix": '{"name": "mcp\\u005f\\u005fcorsia__read_file", "arguments": {"path": "' + PATH + '"}, "turn_id": "turn"}',
    "escaped target": '{"name": "tool_call", "arguments": {"name": "\\u0052ead", "arguments": {"path": "'
                      + PATH + '"}}, "turn_id": "turn"}',
    "duplicate name keys": '{"name": "bash", "name": "read_file", "arguments": {"path": "' + PATH + '"}, "turn_id": "turn"}',
    "whitespace": '{ "name" :\n "read_file" , "arguments" : {"path": "' + PATH + '"}, "turn_id": "turn"}',
    # json.loads takes NaN and Infinity, but they are not canonical JSON: SQLite 3.44.0 made
    # one-argument json_valid() report them false again, so only the JSON5-aware guard walks them.
    "non-canonical NaN": '{"name": "read_file", "arguments": {"path": "' + PATH + '"}, "turn_id": "turn", "cost": NaN}',
    "non-canonical Infinity": '{"name": "read_file", "arguments": {"path": "' + PATH + '"}, "turn_id": "turn", "cost": -Infinity}',
}
UNREADABLE = {"empty": "", "null": None, "not json": "{not json", "string": json.dumps("read_file")}


def _all_rows(db, thread="owner"):
    return db.execute(
        "SELECT m.id,m.thread_id,m.metadata FROM threads t JOIN messages m ON m.thread_id=t.id "
        "WHERE t.user_id='user' AND t.id=? AND m.role='tool' ORDER BY m.thread_id,m.seq,m.id", (thread,)
    ).fetchall()


def _matching(db, thread="owner", **kwargs):
    return [row["id"] for row in db.execute(
        "SELECT m.id FROM messages m WHERE m.thread_id=? AND m.role='tool' AND "
        + read_file_metadata_predicate("m.metadata", **kwargs) + " ORDER BY m.seq", (thread,))]


def test_candidate_prefilter_keeps_every_row_python_resolves_to_read_file_and_drops_plain_non_reads(db):
    for label, metadata in {**READS, **NOT_READS, **SPELLED, **UNREADABLE}.items():
        _tool_row(db, label, metadata)
    resolved = [row["id"] for row in _all_rows(db) if _read_call(row)[0] is not None]
    kept = set(_matching(db))
    assert set(resolved) >= set(READS) | set(SPELLED), "every named spelling is a real read_file call"
    assert set(resolved) <= kept, "prefilter must be a superset of what Python resolves"
    dropped = set(NOT_READS) | {"empty", "null", "not json", "string"}
    assert not dropped & kept, "non-reads, malformed tool_calls and unreadable metadata are filtered out"
    assert "not json" not in kept


def test_candidate_groups_equal_the_unfiltered_scan_in_the_same_order(db):
    for label, metadata in {**READS, **NOT_READS, **SPELLED, **UNREADABLE}.items():
        _tool_row(db, label, metadata)
    old: dict = {}
    old_unavailable: Counter = Counter()
    for row in _all_rows(db):
        arguments, metadata = _read_call(row)
        if arguments is None or metadata is None:
            continue
        old.setdefault(("owner", metadata["turn_id"], arguments["path"]), []).append(row["id"])
    new_unavailable: Counter = Counter()
    new = _candidate_groups(db, "user", ("owner",), new_unavailable)
    assert {key: [row["id"] for row, _ in value] for key, value in new.items()} == old
    assert new_unavailable == old_unavailable


def test_chunked_rows_keep_order_across_chunk_boundaries(db):
    for index in range(25):
        _tool_row(db, f"row-{index:02d}", _call("read_file", path=PATH), seq=100 - index)
    rows = list(json_row_chunks(
        db, "SELECT id,thread_id,seq FROM messages WHERE role='tool'", (), ("id", "thread_id", "seq"),
        "thread_id,seq,id", size=4,
    ))
    assert [row["id"] for row in rows] == [f"row-{index:02d}" for index in range(24, -1, -1)]
    assert list(json_row_chunks(db, "SELECT id FROM messages WHERE 0", (), ("id",), "id")) == []


# --- receipts: the previous row-by-row implementation is the oracle -------------------------

OLD_USAGE_SQL = (
    "SELECT u.id,u.thread_id,u.turn_id,u.plan_node_id,u.plan_attempt_id,u.plan_commitment_id,"
    "u.context_manifest_id,u.created_at,u.model,m.harness,m.entries_json "
    "FROM threads t JOIN llm_usage u ON u.thread_id=t.id "
    "LEFT JOIN context_manifests m ON m.id=u.context_manifest_id "
    "WHERE t.user_id=? AND t.id IN ({}) ORDER BY u.created_at,u.id"
)


def old_read_offer_evidence(db, *, user_id, thread_ids, from_ts, to_ts):
    retained: dict = {}
    period_offers: list = []
    period_reasons: dict = {}
    period_calls = 0
    owners = tuple(dict.fromkeys(thread_ids))
    for offset in range(0, len(owners), 499):
        chunk = owners[offset:offset + 499]
        if not chunk:
            continue
        rows = db.execute(OLD_USAGE_SQL.format(",".join("?" for _ in chunk)), (user_id, *chunk))
        for usage in rows:
            in_period = from_ts <= usage["created_at"] < to_ts
            if in_period:
                period_calls += 1
            lessons, reason = _lessons(usage["entries_json"])
            if reason:
                if in_period:
                    period_reasons[reason] = period_reasons.get(reason, 0) + 1
                continue
            seen: set = set()
            for lesson in lessons or []:
                identity = lesson["id"], lesson["behavioral_sha256"]
                if identity in seen:
                    continue
                seen.add(identity)
                offer = {
                    "kind": "lesson", "id": identity[0], "version": identity[1],
                    "offered": True, "usage_id": usage["id"], "thread_id": usage["thread_id"],
                    "turn_id": usage["turn_id"], "manifest_id": usage["context_manifest_id"],
                    "task_id": usage["plan_node_id"], "attempt_id": usage["plan_attempt_id"],
                    "commitment_id": usage["plan_commitment_id"], "created_at": usage["created_at"],
                    "model": usage["model"], "harness": usage["harness"],
                    "raw_version": None, "descriptor_tokens": None,
                    "version_reason": "per-call manifest records behavioral identity only",
                }
                retained.setdefault(identity, offer)
                if in_period:
                    period_offers.append(offer)
    return {"retained": list(retained.values()), "period": {"offers": period_offers,
            "calls": period_calls, "unavailable_calls": period_reasons}}


def _lesson(identity, version="a"):
    return {"id": identity, "behavioral_sha256": version * 64}


MANIFESTS = {
    "two": [_lesson("one"), _lesson("two", "b")],
    "duplicate": [_lesson("one"), _lesson("one"), _lesson("two", "b")],
    "new-version": [_lesson("one", "c")],
    "zero": [],
}


def _manifest(db, identity, entries):
    db.execute(
        "INSERT INTO context_manifests(id,harness,entries_json,entry_count,created_at) VALUES(?,'native',?,1,'t')",
        (identity, json.dumps(entries)),
    )


def _usage(db, identity, manifest, created_at, thread="owner", node=None):
    attribution = ("plan", node, "attempt", "commitment", "execution") if node else (None,) * 5
    db.execute(
        "INSERT INTO llm_usage(id,thread_id,turn_id,model,context_manifest_id,created_at,"
        "plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role) VALUES(?,?,'turn','m',?,?,?,?,?,?,?)",
        (identity, thread, manifest, created_at, *attribution),
    )


def _ordered(result):
    """Canonical digest text plus the key order sort_keys hides."""
    return json.dumps(result, sort_keys=True, default=str), list(result["period"]["unavailable_calls"].items())


def _text(offers):
    """The canonical document text of a list of offers, as the fingerprint hashes it."""
    return ",".join(json.dumps(offer, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    for offer in offers).encode()


def _fed(deferred):
    digest = hashlib.sha256()
    deferred.feed(digest)
    return digest.hexdigest()


def assert_materialized_equals_the_scan(new, old):
    """The materialized period offers carry exactly what the row-by-row list did."""
    deferred, scanned = new["period"].pop("offers"), old["period"].pop("offers")
    assert new == old and _ordered(new) == _ordered(old)
    assert_offers_equal(deferred, scanned)


def assert_offers_equal(deferred, scanned):
    """A DeferredOffers hashes to the document text of ``scanned`` and counts what it lists."""
    assert _fed(deferred) == hashlib.sha256(_text(scanned)).hexdigest()
    expected: dict = {}
    for offer in scanned:
        row = expected.setdefault((offer["id"], offer["version"]), {
            "n": 0, "calls": set(), "tasks": set(), "attempts": set(), "calls_without_task": set()})
        row["n"] += 1
        row["calls"].add(offer["usage_id"])
        if offer["task_id"]:
            row["tasks"].add(offer["task_id"])
        if offer["attempt_id"]:
            row["attempts"].add(offer["attempt_id"])
        if not offer["task_id"]:
            row["calls_without_task"].add(offer["usage_id"])
    assert deferred.counts == {
        identity: {"n": row["n"], **{key: len(row[key]) for key in ("calls", "tasks", "attempts", "calls_without_task")}}
        for identity, row in expected.items()}


def test_receipts_match_the_row_by_row_scan_for_every_manifest_shape(db):
    for identity, lessons in MANIFESTS.items():
        _manifest(db, identity, [{"source_id": "dynamic:skills-index", "lessons": lessons}])
    _manifest(db, "no-source", [{"source_id": "other"}])
    _manifest(db, "two-sources", [{"source_id": "dynamic:skills-index", "lessons": []}] * 2)
    _manifest(db, "no-lessons", [{"source_id": "dynamic:skills-index"}])
    _manifest(db, "bad-lesson", [{"source_id": "dynamic:skills-index", "lessons": [{"id": "x"}]}])
    db.execute("INSERT INTO context_manifests(id,harness,entries_json,entry_count,created_at) VALUES('object','n','{}',0,'t')")
    db.execute("INSERT INTO context_manifests(id,harness,entries_json,entry_count,created_at) VALUES('scalar','n','7',0,'t')")
    order = ["no-lessons", "two", "bad-lesson", None, "dangling", "duplicate", "no-source", "object",
             "scalar", "zero", "two-sources", "new-version", "two", "bad-lesson", "no-lessons"]
    stamps = ["2026-09-22T00:00:00", "2026-09-20T00:00:00", "2026-09-23T00:00:00",
              "2026-09-23T00:00:00", "2026-09-24T00:00:00", "2026-09-28T00:00:00"]
    for index, manifest in enumerate(order):
        # created_at ties across calls make (created_at, id) the only order.
        _usage(db, f"call-{len(order) - index:02d}", manifest, stamps[index % len(stamps)],
               node="task" if index % 2 else None)
    kwargs = dict(user_id="user", thread_ids=("owner", "foreign", "owner", "missing"),
                  from_ts="2026-09-21", to_ts="2026-09-28")
    new = read_offer_evidence(db, **kwargs)
    old = old_read_offer_evidence(db, **kwargs)
    assert old["period"]["calls"] and old["period"]["unavailable_calls"] and old["retained"]
    assert_materialized_equals_the_scan(new, old)


def test_receipts_match_across_thread_chunks_and_first_retained_offer(db):
    _manifest(db, "early", [{"source_id": "dynamic:skills-index", "lessons": [_lesson("one")]}])
    _manifest(db, "late", [{"source_id": "dynamic:skills-index", "lessons": [_lesson("one"), _lesson("two")]}])
    _manifest(db, "empty", [{"source_id": "dynamic:skills-index"}])
    threads = []
    for index in range(1100):
        thread = f"t{index:04d}"
        threads.append(thread)
        db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES(?,'user','m','t','t')", (thread,))
        # The later chunk holds the earliest timestamp; retention is still per chunk order.
        stamp = f"2026-09-{30 - index // 50:02d}T00:00:00"
        _usage(db, f"a-{index:04d}", "late" if index % 3 else "early", stamp, thread=thread)
        _usage(db, f"b-{index:04d}", "empty", "2026-09-22T00:00:00", thread=thread)
    kwargs = dict(user_id="user", thread_ids=tuple(threads), from_ts="2026-09-21", to_ts="2026-09-28")
    new = read_offer_evidence(db, **kwargs)
    old = old_read_offer_evidence(db, **kwargs)
    assert len(old["retained"]) == 2 and old["period"]["calls"] > 1000
    assert_materialized_equals_the_scan(new, old)


def test_receipts_with_no_usage_or_no_threads_are_empty(db):
    for threads in ((), ("owner",)):
        kwargs = dict(user_id="user", thread_ids=threads, from_ts="2026-09-21", to_ts="2026-09-28")
        assert_materialized_equals_the_scan(read_offer_evidence(db, **kwargs), old_read_offer_evidence(db, **kwargs))


def test_unparseable_manifest_json_still_raises_like_before(db):
    db.execute("PRAGMA ignore_check_constraints=1")
    db.execute("INSERT INTO context_manifests(id,harness,entries_json,entry_count,created_at) VALUES('bad','n','{oops',0,'t')")
    _usage(db, "call", "bad", "2026-09-22")
    kwargs = dict(user_id="user", thread_ids=("owner",), from_ts="2026-09-21", to_ts="2026-09-28")
    with pytest.raises(json.JSONDecodeError):
        old_read_offer_evidence(db, **kwargs)
    with pytest.raises(json.JSONDecodeError):
        read_offer_evidence(db, **kwargs)
