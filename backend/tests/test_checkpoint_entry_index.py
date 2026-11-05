"""The entry index has one writer, and only the shared reader decides validity."""

from __future__ import annotations

from infra.thread_confinement import Reader

import json

from checkpoint_retrieval_fixtures import (  # noqa: F401
    _install_checkpoint,
    fresh_db,
)


def _entry(entry_id: int, statement: str, **extra) -> dict:
    return {
        "entry_id": entry_id,
        "field": "facts",
        "body": {"statement": statement},
        "revision": 1,
        **extra,
    }


def _index_rows(db) -> list[tuple]:
    return [
        tuple(row)
        for row in db.execute(
            "SELECT entry_id, revision, search_text FROM checkpoint_entry_search "
            "ORDER BY entry_id, revision"
        )
    ]


def test_non_ascii_words_are_found(fresh_db):
    """The index holds words, not JSON escapes, so an accented term matches."""
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db,
        thread_id="t1",
        narrative="Routine.",
        continuity=[_entry(1, "The café menu is pinned.")],
    )

    hits = search_checkpoints(fresh_db, "café", reader=Reader.agent("t1"))

    assert [hit["kind"] for hit in hits] == ["continuity_entry"]
    assert _index_rows(fresh_db) == [(1, 1, "The café menu is pinned.")]


def test_search_text_carries_every_human_field_and_no_structure():
    from core.continuity_render import ContinuitySnapshotEntry

    entry = ContinuitySnapshotEntry(
        entry_id=1,
        field="verification",
        body={
            "result": "passed",
            "evidence": "ran suite",
            "source_refs": [{"kind": "turn", "reference": "t-9"}],
        },
        revision=2,
    )

    assert entry.search_text == "passed ran suite"


def test_snapshot_the_reader_rejects_indexes_nothing(fresh_db):
    """One malformed sibling makes the whole snapshot unreadable, so no rows."""
    good = _entry(1, "Valid sibling entry.")
    bad = {"entry_id": 2, "field": "facts", "body": {"statement": "x"},
           "revision": "not-an-int"}
    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Routine.", continuity=[good, bad]
    )

    assert _index_rows(fresh_db) == []


def test_entry_without_a_revision_is_indexed(fresh_db):
    """Whatever the reader accepts is indexed, including a revision-less entry."""
    entry = {"entry_id": 1, "field": "facts", "body": {"statement": "Legacy."}}
    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Routine.", continuity=[entry]
    )
    from memory.checkpoint_search import search_checkpoints

    assert _index_rows(fresh_db) == [(1, 0, "Legacy.")]
    hits = search_checkpoints(fresh_db, "legacy", reader=Reader.agent("t1"))
    assert [hit["kind"] for hit in hits] == ["continuity_entry"]


def test_backfill_runs_the_same_writer_idempotently_and_counts(fresh_db):
    from infra.checkpoint_entry_index import backfill_checkpoint_entry_search

    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="One.", continuity=[_entry(1, "Alpha.")],
        checkpoint_id="cp-a",
    )
    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Two.", continuity=[_entry(1, "Alpha.")],
        checkpoint_id="cp-b",
    )
    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Empty.", continuity=[],
        checkpoint_id="cp-empty",
    )
    _install_checkpoint(
        fresh_db, thread_id="t2", narrative="Bad.", continuity=[],
        checkpoint_id="cp-bad",
    )
    fresh_db.execute(
        "UPDATE checkpoints SET continuity_snapshot_json=? WHERE id='cp-bad'",
        (json.dumps({"not": "a list"}),),
    )
    before = _index_rows(fresh_db)

    first = backfill_checkpoint_entry_search(fresh_db)
    second = backfill_checkpoint_entry_search(fresh_db)

    assert (first.indexed, first.rejected, first.skipped) == (2, 1, 1)
    assert (second.indexed, second.rejected, second.skipped) == (2, 1, 1)
    assert _index_rows(fresh_db) == before == [(1, 1, "Alpha.")]
    pointers = fresh_db.execute(
        "SELECT first_generation_order, last_generation_order "
        "FROM checkpoint_entry_search"
    ).fetchone()
    assert tuple(pointers) == (1, 2)


def test_hits_merge_by_relevance_with_generation_order_as_tiebreak(
    fresh_db, monkeypatch
):
    """A narrative hit can outrank an entry hit; equal relevance prefers newer."""
    import memory.checkpoint_search as search

    def hit(kind: str, name: str) -> dict:
        return {"kind": kind, "name": name, "thread_id": "t1", "historical": False}

    monkeypatch.setattr(search, "_entry_rows", lambda *a, **k: [])
    monkeypatch.setattr(search, "_narrative_rows", lambda *a, **k: [])
    monkeypatch.setattr(search, "_entry_hits", lambda rows: [
        (-1.0, 3, hit("continuity_entry", "entry-weak")),
        (-2.0, 1, hit("continuity_entry", "entry-tie-old")),
    ])
    monkeypatch.setattr(search, "_narrative_hits", lambda rows: [
        (-5.0, 2, hit("narrative", "narrative-strong")),
        (-2.0, 4, hit("narrative", "narrative-tie-new")),
    ])

    hits = search.search_checkpoints(
        fresh_db, "anything", reader=Reader.agent("t1"), limit=5, per_thread=5
    )

    assert [h["name"] for h in hits] == [
        "narrative-strong", "narrative-tie-new", "entry-tie-old", "entry-weak",
    ]


def test_stale_index_row_is_dropped_with_a_distinct_log_reason(fresh_db):
    """An index row whose entry is not in the decoded snapshot never surfaces."""
    from loguru import logger
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Routine.",
        continuity=[_entry(1, "Ghostword entry.")],
    )
    fresh_db.execute(
        "UPDATE checkpoints SET continuity_snapshot_json='[]'"
    )
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        hits = search_checkpoints(fresh_db, "ghostword", reader=Reader.agent("t1"))
    finally:
        logger.remove(sink)

    assert hits == []
    assert any("stale_entry_index_row" in message for message in messages)


def test_install_is_the_write_seam_and_purge_the_removal_seam(monkeypatch):
    """Installing indexes the frozen entries; purging a thread removes them."""
    import sqlite3

    import infra.checkpoints as checkpoints
    from infra.checkpoint_entry_index import delete_thread_checkpoints
    from infra.checkpoints import capture, create_running
    from infra.db_fts import _init_checkpoint_entry_search_fts
    from infra.schema import SCHEMA_SQL
    from test_checkpoint_store import _request, _seed_thread

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    _init_checkpoint_entry_search_fts(db)
    _seed_thread(db, "thread-1")
    snapshot = json.dumps([_entry(1, "Installed through the seam.")])
    monkeypatch.setattr(checkpoints, "merge_staged_edits", lambda *a, **k: snapshot)
    running = create_running(_request("thread-1", "cp-1"), db=db)
    capture("cp-1", raw_result="raw", usage_json="{}", captured_at="t2", db=db)
    assert _index_rows(db) == []

    checkpoints.install("cp-1", installed_at="t3", db=db)

    assert running.id == "cp-1"
    assert _index_rows(db) == [(1, 1, "Installed through the seam.")]
    delete_thread_checkpoints(db, "thread-1")
    assert _index_rows(db) == []


def test_index_executes_large_snapshots_in_bounded_upserts(fresh_db):
    """A 201-entry snapshot uses 100-row UPSERTs and a one-row final batch."""
    from infra.checkpoint_entry_index import index_checkpoint_entries

    entries = [_entry(number, f"Entry {number}.") for number in range(1, 202)]
    checkpoint_id = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Large.", continuity=entries,
        status="captured", checkpoint_id="cp-large",
    )
    fresh_db.execute("UPDATE checkpoints SET status='installed' WHERE id=?", (checkpoint_id,))
    class ExecutionCounter:
        """Count UPSERT executions, including each row stepped by executemany."""

        def __init__(self, connection):
            self.connection = connection
            self.executions: list[str] = []

        def execute(self, sql, parameters=()):
            self.executions.append(sql)
            return self.connection.execute(sql, parameters)

        def executemany(self, sql, parameters):
            batch = list(parameters)
            self.executions.extend([sql] * len(batch))
            return self.connection.executemany(sql, batch)

    counted = ExecutionCounter(fresh_db)
    outcome = index_checkpoint_entries(counted, checkpoint_id)

    inserts = [
        sql for sql in counted.executions
        if sql.startswith("INSERT INTO checkpoint_entry_search(")
    ]
    assert outcome.indexed == 201
    assert [sql.count("(?,?,?,?,?,?,?,?)") for sql in inserts] == [100, 100, 1]
    assert len(_index_rows(fresh_db)) == 201


def test_duplicate_keys_in_one_batch_match_row_by_row_upsert(fresh_db):
    """Repeated frozen keys retain their first text and widen generation pointers."""
    from infra.checkpoint_entry_index import index_checkpoint_entries
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Before.",
        continuity=[_entry(1, "Original indexed text.")], checkpoint_id="cp-before",
    )
    checkpoint_id = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="After.", status="captured",
        continuity=[
            _entry(1, "First duplicate replacement."),
            _entry(1, "Second duplicate replacement."),
            _entry(2, "First new duplicate."),
            _entry(2, "Second new duplicate."),
        ],
        checkpoint_id="cp-duplicates",
    )
    before_order = fresh_db.execute(
        "SELECT generation_order FROM checkpoints WHERE id='cp-before'"
    ).fetchone()[0]
    duplicate_order = fresh_db.execute(
        "SELECT generation_order FROM checkpoints WHERE id=?", (checkpoint_id,)
    ).fetchone()[0]
    fresh_db.execute("UPDATE checkpoints SET status='installed' WHERE id=?", (checkpoint_id,))

    index_checkpoint_entries(fresh_db, checkpoint_id)

    rows = fresh_db.execute(
        "SELECT entry_id,revision,field,search_text,first_generation_order,"
        "last_generation_order FROM checkpoint_entry_search ORDER BY entry_id,revision"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (1, 1, "facts", "Original indexed text.", before_order, duplicate_order),
        (2, 1, "facts", "First new duplicate.", duplicate_order, duplicate_order),
    ]
    assert [hit["kind"] for hit in search_checkpoints(
        fresh_db, "original", reader=Reader.agent("t1")
    )] == ["continuity_entry"]
    assert search_checkpoints(
        fresh_db, "replacement", reader=Reader.agent("t1")
    ) == []
    assert [hit["kind"] for hit in search_checkpoints(
        fresh_db, "new", reader=Reader.agent("t1")
    )] == ["continuity_entry"]
