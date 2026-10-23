"""Lesson-report receipt scans stream exactly the direct receipt reference rows."""
from __future__ import annotations

import hashlib
import json
from collections import Counter

from core.lesson_report_receipts import read_offer_evidence
from core.lesson_report_reads import _candidate_groups, _read_call
from core.lesson_report_sql import json_row_chunks, read_file_metadata_predicate
from tests.lesson_receipt_helpers import write_trial_receipt
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
    "escaped underscore": '{"name": "read\\u005ffile", "arguments": {"path": "' + PATH + '"}, "turn_id": "turn"}',
    "escaped alias": '{"name": "\\u0052ead", "arguments": {"path": "' + PATH + '"}, "turn_id": "turn"}',
    "escaped prefix": '{"name": "mcp\\u005f\\u005fcorsia__read_file", "arguments": {"path": "' + PATH + '"}, "turn_id": "turn"}',
    "escaped target": '{"name": "tool_call", "arguments": {"name": "\\u0052ead", "arguments": {"path": "'
                      + PATH + '"}}, "turn_id": "turn"}',
    "duplicate name keys": '{"name": "bash", "name": "read_file", "arguments": {"path": "' + PATH + '"}, "turn_id": "turn"}',
    "whitespace": '{ "name" :\n "read_file" , "arguments" : {"path": "' + PATH + '"}, "turn_id": "turn"}',
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
        + read_file_metadata_predicate("m.metadata", **kwargs) + " ORDER BY m.seq", (thread,)
    )]


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


# --- receipts: direct row-by-row receipt reference -------------------------------------------

_RECEIPT_FIELDS = (
    "receipt_id", "thread_id", "turn_id", "task_id", "checkpoint_generation_id", "created_at", "path", "raw_sha256",
    "id", "version",
)


def receipt_reference(db, *, user_id, thread_ids, from_ts, to_ts):
    """The receipt rows the report is allowed to expose, independent of its chunking."""
    owners = tuple(dict.fromkeys(thread_ids))
    if not owners:
        return {"retained": [], "period": {"offers": [], "calls": 0, "unavailable_calls": {}}}
    owner_sql = ",".join("?" for _ in owners)
    owner_chunks = {owner: index // 499 for index, owner in enumerate(owners)}
    rows = list(db.execute(
        "SELECT g.id AS receipt_id,g.thread_id,g.turn_id,g.node_id AS task_id,g.checkpoint_generation_id,"
        "g.created_at,g.path,g.raw_sha256,g.lesson_id AS id,g.lesson_behavioral_sha256 AS version "
        "FROM guidance_loads g JOIN threads t ON t.id=g.thread_id "
        f"WHERE t.user_id=? AND t.id IN ({owner_sql}) AND g.variant='with' "
        "AND g.lesson_id IS NOT NULL AND g.lesson_behavioral_sha256 IS NOT NULL",
        (user_id, *owners),
    ))
    rows.sort(key=lambda row: (owner_chunks[row["thread_id"]], row["created_at"], row["receipt_id"]))
    offers = [_offer(dict(row)) for row in rows if from_ts <= row["created_at"] < to_ts]
    retained: dict[tuple[str, str, str, str], dict] = {}
    for row in sorted(rows, key=lambda row: (row["created_at"], row["receipt_id"])):
        offer = _offer(dict(row))
        retained.setdefault((offer["id"], offer["version"], offer["path"], offer["raw_sha256"]), offer)
    calls = db.execute(
        "SELECT COUNT(*) FROM llm_usage u JOIN threads t ON t.id=u.thread_id "
        f"WHERE t.user_id=? AND t.id IN ({owner_sql}) AND u.created_at>=? AND u.created_at<?",
        (user_id, *owners, from_ts, to_ts),
    ).fetchone()[0]
    return {"retained": list(retained.values()), "period": {"offers": offers, "calls": calls, "unavailable_calls": {}}}


def _offer(row):
    return {
        "kind": "lesson", "id": row["id"], "version": row["version"], "offered": True,
        **{field: row[field] for field in _RECEIPT_FIELDS}, "raw_version": None, "descriptor_tokens": None,
        "version_reason": "task-start receipt records behavioral identity",
    }


def _text(offers):
    """The canonical document text of a list of offers, as the fingerprint hashes it."""
    return ",".join(json.dumps(offer, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    for offer in offers).encode()


def _fed(deferred):
    digest = hashlib.sha256()
    deferred.feed(digest)
    return digest.hexdigest()


def assert_materialized_equals_the_scan(new, reference):
    """The streamed report rows are exactly the direct receipt reference rows."""
    deferred = new["period"]["offers"]
    assert new["retained"] == reference["retained"]
    assert new["period"] | {"offers": reference["period"]["offers"]} == reference["period"]
    assert_offers_equal(deferred, reference["period"]["offers"])


def assert_offers_equal(deferred, scanned):
    """A DeferredOffers hashes to the receipt document text and counts its receipts and tasks."""
    assert _fed(deferred) == hashlib.sha256(_text(scanned)).hexdigest()
    expected: dict = {}
    for offer in scanned:
        row = expected.setdefault((offer["id"], offer["version"]), {"n": 0, "receipts": set(), "tasks": set()})
        row["n"] += 1
        row["receipts"].add(offer["receipt_id"])
        if offer["task_id"]:
            row["tasks"].add(offer["task_id"])
    assert deferred.counts == {
        identity: {"n": row["n"], **{key: len(row[key]) for key in ("receipts", "tasks")}}
        for identity, row in expected.items()
    }


def test_receipts_match_the_row_by_row_reference_and_exclude_non_with_or_incomplete_rows(db):
    write_trial_receipt(db, lesson_id="one", node_id="task-a", created_at="2026-09-22T00:00:00")
    write_trial_receipt(db, lesson_id="two", behavioral_sha256="b" * 64, node_id="task-b", created_at="2026-09-23T00:00:00")
    write_trial_receipt(db, lesson_id="one", node_id="task-a", created_at="2026-09-24T00:00:00")
    write_trial_receipt(db, lesson_id="outside", created_at="2026-09-20T00:00:00")
    write_trial_receipt(db, lesson_id="without", variant="without", created_at="2026-09-22T00:00:01")
    write_trial_receipt(db, lesson_id="crossover", variant="crossover", created_at="2026-09-22T00:00:02")
    missing_lesson = write_trial_receipt(db, lesson_id="missing", created_at="2026-09-22T00:00:03")
    missing_hash = write_trial_receipt(db, lesson_id="missing-hash", created_at="2026-09-22T00:00:04")
    db.execute("UPDATE guidance_loads SET lesson_id=NULL WHERE id=?", (missing_lesson,))
    db.execute("UPDATE guidance_loads SET lesson_behavioral_sha256=NULL WHERE id=?", (missing_hash,))
    kwargs = dict(user_id="user", thread_ids=("owner", "foreign", "owner", "missing"),
                  from_ts="2026-09-21", to_ts="2026-09-28")
    new = read_offer_evidence(db, **kwargs)
    reference = receipt_reference(db, **kwargs)
    assert [offer["id"] for offer in reference["period"]["offers"]] == ["one", "two", "one"]
    assert_materialized_equals_the_scan(new, reference)


def test_receipts_match_across_thread_chunks_and_keep_first_retained_receipt(db):
    threads = []
    for index in range(1100):
        thread = f"t{index:04d}"
        threads.append(thread)
        db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES(?,'user','m','t','t')", (thread,))
        # The later chunk holds the earliest timestamp; retention still keeps the earliest receipt.
        stamp = f"2026-09-{30 - index // 50:02d}T00:00:00"
        write_trial_receipt(db, lesson_id="one", node_id=f"task-{index}", created_at=stamp, thread_id=thread)
        if index % 3:
            write_trial_receipt(db, lesson_id="two", node_id=f"task-{index}", created_at="2026-09-22T00:00:00", thread_id=thread)
    kwargs = dict(user_id="user", thread_ids=tuple(threads), from_ts="2026-09-21", to_ts="2026-09-28")
    new = read_offer_evidence(db, **kwargs)
    reference = receipt_reference(db, **kwargs)
    assert len(reference["retained"]) == 2 and reference["retained"][0]["id"] == "one"
    assert reference["retained"][0]["created_at"] == "2026-09-09T00:00:00"
    assert len(reference["period"]["offers"]) > 1000
    assert_materialized_equals_the_scan(new, reference)


def test_receipts_with_no_rows_or_no_threads_are_empty(db):
    for threads in ((), ("owner",)):
        kwargs = dict(user_id="user", thread_ids=threads, from_ts="2026-09-21", to_ts="2026-09-28")
        assert_materialized_equals_the_scan(read_offer_evidence(db, **kwargs), receipt_reference(db, **kwargs))


# Disposition: the manifest-JSON failure test is deleted because receipt-native offers never parse manifests.
