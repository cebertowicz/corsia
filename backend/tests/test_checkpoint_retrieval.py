"""Bounded retrieval over retained checkpoints.

Punch-list Task 5, first delivery boundary. Checkpoint generations already
carry a narrative and frozen continuity at an exact message boundary; nothing
could search them, so an agent asking "what did we decide and why" had to
reread raw history. These tests pin the retrieval contract before it exists.

The contract is deliberately narrow. Search never promotes a historical
narrative into current authority, so every result carries provenance and the
generation time, and an exact read that cannot be served says so rather than
guessing or silently widening its search.
"""

from __future__ import annotations

from checkpoint_retrieval_fixtures import _install_checkpoint, fresh_db

# Re-exported so pytest resolves the fixture in this module's tests.
__all__ = ["fresh_db", "_install_checkpoint"]


def test_narrative_is_searchable(fresh_db):
    """A question about earlier work finds the generation that discussed it."""
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db,
        thread_id="t1",
        narrative="We rejected the polling design and chose event routing.",
    )

    hits = search_checkpoints(fresh_db, "polling", caller_thread_id="t1", limit=5)

    assert len(hits) == 1
    assert hits[0]["thread_id"] == "t1"
    assert "polling" in hits[0]["narrative"]


def test_frozen_continuity_is_searchable(fresh_db):
    """Facts live in continuity entries, not only in the narrative prose."""
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db,
        thread_id="t1",
        narrative="Routine progress.",
        continuity=[{"statement": "The staging bucket is eu-west-1."}],
    )

    hits = search_checkpoints(fresh_db, "eu-west-1", caller_thread_id="t1", limit=5)

    assert len(hits) == 1
    assert hits[0]["thread_id"] == "t1"


def test_older_epoch_is_retrievable_but_labeled_historical(fresh_db):
    """A project-move boundary preserves history without calling it current."""
    from memory.checkpoint_search import get_checkpoint, search_checkpoints

    cid = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Budget is 400 dollars.",
        history_epoch=0,
    )
    fresh_db.execute("UPDATE threads SET history_epoch=1 WHERE id='t1'")
    fresh_db.commit()

    hit = search_checkpoints(fresh_db, "budget", caller_thread_id="t1", limit=5)[0]
    exact = get_checkpoint(fresh_db, cid, caller_thread_id="t1")

    assert hit["historical"] is True and hit["history_epoch"] == 0
    assert exact["historical"] is True and exact["history_epoch"] == 0


def test_entry_hit_collapses_generations_and_reports_canonical_revision(fresh_db):
    """One entry result retains pointers and reports canonical revision state."""
    from memory.checkpoint_search import search_checkpoints

    original = {
        "entry_id": 7,
        "field": "decisions",
        "body": {"statement": "Use event routing for all retries."},
        "revision": 1,
    }
    replacement = {
        "entry_id": 7,
        "field": "decisions",
        "body": {"statement": "Use queue routing for all retries."},
        "revision": 2,
    }
    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Routine.", continuity=[original],
        checkpoint_id="cp-first",
    )
    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Routine.", continuity=[original],
        checkpoint_id="cp-middle",
    )
    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Routine.", continuity=[replacement],
        checkpoint_id="cp-latest",
    )

    hit = search_checkpoints(fresh_db, "event routing", caller_thread_id="t1")[0]

    assert hit == {
        "kind": "continuity_entry",
        "thread_id": "t1",
        "statement": "Use event routing for all retries.",
        "first_checkpoint": {"checkpoint_id": "cp-first", "generated_at": "2026-09-01T00:05:01"},
        "latest_checkpoint": {"checkpoint_id": "cp-middle", "generated_at": "2026-09-01T00:05:01"},
        "entry_state": "revised",
        "current_revision": 2,
        "historical": False,
        "history_epoch": 0,
        "entry_id": 7,
        "field": "decisions",
    }


def test_entry_hit_reports_retirement(fresh_db):
    """An entry absent from a later generation is reported as retired."""
    from memory.checkpoint_search import search_checkpoints

    entry = {
        "entry_id": 9,
        "field": "facts",
        "body": {"statement": "The staging region is eu-west-1."},
        "revision": 1,
    }
    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Routine.", continuity=[entry],
        checkpoint_id="cp-active-entry",
    )
    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Routine.", continuity=[],
        checkpoint_id="cp-retired-entry",
    )
    fresh_db.execute(
        "UPDATE continuity_entries SET state='retired', retired_reason='superseded' "
        "WHERE thread_id='t1' AND history_epoch=0 AND entry_id=9"
    )
    fresh_db.commit()

    hit = search_checkpoints(fresh_db, "staging region", caller_thread_id="t1")[0]

    assert hit["entry_state"] == "retired"
    assert hit["retired_reason"] == "superseded"


def test_entry_state_is_missing_when_the_canonical_row_is_gone(fresh_db):
    """Frozen evidence remains searchable when its canonical lifecycle row is gone."""
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db,
        thread_id="t1",
        narrative="Routine.",
        continuity=[{"entry_id": 15, "field": "facts", "body": {"statement": "Memory is durable."}, "revision": 1}],
    )
    fresh_db.execute(
        "DELETE FROM continuity_entries WHERE thread_id='t1' AND history_epoch=0 AND entry_id=15"
    )
    fresh_db.commit()

    hit = search_checkpoints(fresh_db, "memory durable", caller_thread_id="t1")[0]

    assert hit["entry_state"] == "missing"


def test_same_entry_id_in_different_epochs_does_not_deduplicate(fresh_db):
    """Entry ids are only stable inside one thread history epoch."""
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db,
        thread_id="t1",
        history_epoch=0,
        narrative="Routine.",
        continuity=[{"entry_id": 1, "field": "facts", "body": {"statement": "Routing started in epoch zero."}, "revision": 1}],
        checkpoint_id="cp-epoch-zero",
    )
    fresh_db.execute("UPDATE threads SET history_epoch=1 WHERE id='t1'")
    _install_checkpoint(
        fresh_db,
        thread_id="t1",
        history_epoch=1,
        narrative="Routine.",
        continuity=[{"entry_id": 1, "field": "facts", "body": {"statement": "Routing resumed in epoch one."}, "revision": 1}],
        checkpoint_id="cp-epoch-one",
    )

    hits = search_checkpoints(
        fresh_db, "routing", caller_thread_id="t1", per_thread=2
    )

    assert {(hit["history_epoch"], hit["entry_id"]) for hit in hits} == {(0, 1), (1, 1)}


def test_deleted_pointer_checkpoint_removes_its_entry_index_row(fresh_db):
    """Entry pointers cannot survive the checkpoint rows they reference."""
    from memory.checkpoint_search import search_checkpoints

    checkpoint_id = _install_checkpoint(
        fresh_db,
        thread_id="t1",
        narrative="Routine.",
        continuity=[{"statement": "Pointer evidence remains safe."}],
    )
    fresh_db.execute("DELETE FROM checkpoints WHERE id=?", (checkpoint_id,))
    fresh_db.commit()

    assert search_checkpoints(fresh_db, "pointer", caller_thread_id="t1") == []


def test_search_uses_canonical_result_primary_text(fresh_db):
    """Non-statement entries use the same primary text as checkpoint rendering."""
    from memory.checkpoint_search import search_checkpoints

    entry = {
        "entry_id": 10,
        "field": "verification",
        "body": {"result": "The migration verification passed."},
        "revision": 1,
    }
    _install_checkpoint(fresh_db, thread_id="t1", narrative="Routine.", continuity=[entry])

    hit = search_checkpoints(fresh_db, "migration verification", caller_thread_id="t1")[0]

    assert hit["kind"] == "continuity_entry"
    assert hit["statement"] == "The migration verification passed."


def test_entry_fts_ranks_a_full_match_over_an_earlier_partial_match(fresh_db):
    """One entry-level BM25 ranking cannot retain snapshot order as relevance."""
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db,
        thread_id="t1",
        narrative="Routine.",
        continuity=[
            {"statement": "Event retries remain deferred."},
            {"statement": "Event routing handles retries."},
        ],
    )

    hit = search_checkpoints(fresh_db, "event routing", caller_thread_id="t1")[0]

    assert hit["statement"] == "Event routing handles retries."


def test_query_terms_split_across_entries_fall_back_to_narrative_evidence(fresh_db):
    """Entry FTS needs one matching entry; broad narrative evidence remains readable."""
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db,
        thread_id="t1",
        narrative="Durable checkpoint evidence is retained.",
        continuity=[
            {"statement": "Durable memory is retained."},
            {"statement": "Checkpoint evidence is searchable."},
        ],
    )

    hits = search_checkpoints(
        fresh_db, "durable checkpoint", caller_thread_id="t1", limit=2, per_thread=2
    )

    assert [hit["kind"] for hit in hits] == ["narrative"]
    assert "Durable checkpoint" in hits[0]["narrative"]


def test_unreadable_candidate_snapshot_remains_visible(fresh_db):
    """A checkpoint whose snapshot became unreadable still answers by narrative.

    Its now-stale entry row is dropped rather than reported (see
    test_stale_index_row_is_dropped_with_a_distinct_log_reason), so the
    narrative hit carries the unreadable flag.
    """
    from memory.checkpoint_search import search_checkpoints

    corrupt = _install_checkpoint(
        fresh_db,
        thread_id="t1",
        narrative="Routine staging region notes.",
        continuity=[{"statement": "The staging region is eu-west-1."}],
        checkpoint_id="cp-corrupt",
    )
    fresh_db.execute("DROP TRIGGER checkpoints_fts_update")
    fresh_db.execute(
        "UPDATE checkpoints SET continuity_snapshot_json='{oops' WHERE id=?", (corrupt,)
    )
    fresh_db.commit()

    hit = search_checkpoints(fresh_db, "staging region", caller_thread_id="t1")[0]

    assert hit["kind"] == "narrative"
    assert hit["checkpoint_id"] == corrupt
    assert hit["continuity_unreadable"] is True


def test_uninstalled_generations_are_not_returned(fresh_db):
    """Only a generation that actually installed represents the thread."""
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Abandoned attempt about kafka.",
        status="failed",
    )

    assert search_checkpoints(fresh_db, "kafka", caller_thread_id="t1", limit=5) == []


def test_one_hit_per_thread_by_default(fresh_db):
    """Cumulative narratives repeat; one thread should not flood the result."""
    from memory.checkpoint_search import search_checkpoints

    for n in range(3):
        _install_checkpoint(
            fresh_db,
            thread_id="t1",
            narrative=f"Iteration {n} of the migration plan.",
            checkpoint_id=f"cp-{n}",
        )

    hits = search_checkpoints(fresh_db, "migration", caller_thread_id="t1", limit=5)

    assert len(hits) == 1, "expected the newest generation for the thread only"
    assert hits[0]["checkpoint_id"] == "cp-2"


def test_candidate_ranking_keeps_other_owned_threads_in_scope(fresh_db):
    """A long cumulative thread cannot fill the bounded candidate window."""
    from memory.checkpoint_search import search_checkpoints

    fresh_db.execute(
        "INSERT INTO threads (id,user_id,title,history_epoch) VALUES ('t2','u','T2',0)"
    )
    _install_checkpoint(
        fresh_db, thread_id="t2", narrative="Shared migration context.", checkpoint_id="cp-t2"
    )
    for n in range(80):
        _install_checkpoint(
            fresh_db,
            thread_id="t1",
            narrative="Shared migration context.",
            checkpoint_id=f"cp-t1-{n}",
        )

    hits = search_checkpoints(fresh_db, "migration", caller_thread_id="t1", limit=2)

    assert {hit["thread_id"] for hit in hits} == {"t1", "t2"}


def test_entry_relevance_outranks_newer_cumulative_narrative(fresh_db):
    """A relevant entry ranks ahead of a newer but broad narrative match."""
    from memory.checkpoint_search import search_checkpoints

    entry = {
        "entry_id": 12,
        "field": "facts",
        "body": {"statement": "Durable memory writes checkpoint evidence."},
        "revision": 1,
    }
    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Routine.", continuity=[entry],
        checkpoint_id="cp-relevant",
    )
    _install_checkpoint(
        fresh_db,
        thread_id="t1",
        narrative=("Durable memory writes checkpoint evidence. " + "filler " * 200),
        checkpoint_id="cp-newer-broad",
    )

    hit = search_checkpoints(
        fresh_db, "durable memory writes checkpoint", caller_thread_id="t1"
    )[0]

    assert hit["kind"] == "continuity_entry"
    assert hit["first_checkpoint"]["checkpoint_id"] == "cp-relevant"


def test_search_logs_one_reason_to_a_real_log_sink(fresh_db):
    """The configured backend logger receives the search outcome record."""
    from loguru import logger
    from memory.checkpoint_search import search_checkpoints

    records: list[str] = []
    sink = logger.add(records.append, level="INFO", format="{message}")
    try:
        _install_checkpoint(fresh_db, thread_id="t1", narrative="Current routing.")
        search_checkpoints(fresh_db, "routing", caller_thread_id="t1")
    finally:
        logger.remove(sink)

    reasons = [record for record in records if "checkpoint_search reason=" in record]
    assert reasons == [
        "checkpoint_search reason=hits_current_only hits=1 current_hits=1 historical_hits=0\n"
    ]


def test_search_logs_an_unresolved_caller_separately_from_no_hits(fresh_db):
    """An access refusal is observable without changing the empty result shape."""
    from loguru import logger
    from memory.checkpoint_search import search_checkpoints

    records: list[str] = []
    sink = logger.add(records.append, level="INFO", format="{message}")
    try:
        search_checkpoints(fresh_db, "routing", caller_thread_id="unknown-thread")
    finally:
        logger.remove(sink)

    assert [record for record in records if "checkpoint_search reason=" in record] == [
        "checkpoint_search reason=unresolved_caller hits=0 current_hits=0 historical_hits=0\n"
    ]


def test_results_carry_provenance_and_generation_time(fresh_db):
    """A result must be citable: which thread, which generation, and when."""
    from memory.checkpoint_search import search_checkpoints

    cid = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Chose SQLite over Postgres.",
    )

    hit = search_checkpoints(fresh_db, "postgres", caller_thread_id="t1", limit=5)[0]

    assert hit["checkpoint_id"] == cid
    assert hit["thread_id"] == "t1"
    assert hit["through_seq"] == 100
    assert hit["generated_at"].startswith("2026-09-01")


def test_exact_read_returns_the_generation(fresh_db):
    """The exact selection resolves its own owning thread."""
    from memory.checkpoint_search import get_checkpoint

    cid = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="A long narrative body.",
    )

    got = get_checkpoint(fresh_db, cid, caller_thread_id="t1")

    assert got["checkpoint_id"] == cid
    assert got["thread_id"] == "t1"
    assert got["narrative"] == "A long narrative body."


def test_missing_exact_read_is_an_explicit_typed_error(fresh_db):
    """Unavailable must not be guessed at, widened, or blamed on the caller."""
    from memory.checkpoint_search import get_checkpoint

    got = get_checkpoint(fresh_db, "cp-does-not-exist", caller_thread_id="t1")

    assert got["error_code"] == "not_found"
    assert got["next_action"] == "verify_source_result"
    assert "does not distinguish missing from inaccessible" in got["message"]


def test_exact_read_pages_by_bytes(fresh_db):
    """A large narrative is readable without a full-history reread."""
    from memory.checkpoint_search import get_checkpoint

    cid = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="ABCDEFGHIJ" * 10,
    )

    page = get_checkpoint(fresh_db, cid, caller_thread_id="t1", offset_bytes=10, limit_bytes=5)

    assert page["narrative"] == "ABCDE"
    assert page["offset_bytes"] == 10
    assert page["total_bytes"] == 100


def test_recent_listing_is_newest_first_and_bounded(fresh_db):
    """Listing a thread's generations answers 'where did we get to'."""
    from memory.checkpoint_search import recent_checkpoints

    for n in range(4):
        _install_checkpoint(
            fresh_db, thread_id="t1", narrative=f"Step {n}.",
            checkpoint_id=f"cp-{n}",
        )

    got = recent_checkpoints(fresh_db, "t1", caller_thread_id="t1", limit=2)

    assert [g["checkpoint_id"] for g in got] == ["cp-3", "cp-2"]


def test_tool_refuses_both_selections_at_once():
    """The two selections are exclusive; ambiguity is refused, not guessed."""
    import asyncio

    from tools.query_checkpoints import GetCheckpointsTool

    out = asyncio.run(
        GetCheckpointsTool().execute(checkpoint_id="cp-1", thread_id="t1")
    )

    assert "exclusive" in out


def test_tool_description_steers_away_from_current_state():
    """The description must not invite using history as current authority."""
    from tools.query_checkpoints import GetCheckpointsTool

    description = GetCheckpointsTool().description

    assert "not current authority" in description
    assert "DO NOT use this for" in description


def test_malformed_continuity_does_not_break_a_generation(fresh_db):
    """A bad snapshot must not make an otherwise good narrative unreadable."""
    from memory.checkpoint_search import get_checkpoint

    cid = _install_checkpoint(fresh_db, thread_id="t1", narrative="Body.")
    fresh_db.execute(
        "UPDATE checkpoints SET continuity_snapshot_json='{oops' WHERE id=?",
        (cid,),
    )
    fresh_db.commit()

    got = get_checkpoint(fresh_db, cid, caller_thread_id="t1")

    assert got["narrative"] == "Body."
    assert got["continuity"] == []


def test_exact_read_labels_a_superseded_epoch_historical(fresh_db):
    """A known id remains readable history without becoming current trajectory."""
    from memory.checkpoint_search import get_checkpoint

    cid = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Budget is 400 dollars.",
        history_epoch=0,
    )
    fresh_db.execute("UPDATE threads SET history_epoch=1 WHERE id='t1'")
    fresh_db.commit()

    got = get_checkpoint(fresh_db, cid, caller_thread_id="t1")

    assert got["historical"] is True
    assert got["history_epoch"] == 0


def test_byte_paging_is_actually_bytes(fresh_db):
    """offset_bytes/total_bytes must mean bytes, or callers skip content."""
    from memory.checkpoint_search import get_checkpoint

    # 10 characters, 20 bytes in UTF-8.
    cid = _install_checkpoint(fresh_db, thread_id="t1", narrative="é" * 10)

    got = get_checkpoint(fresh_db, cid, caller_thread_id="t1", offset_bytes=0, limit_bytes=4)

    assert got["total_bytes"] == 20
    assert len(got["narrative"].encode("utf-8")) <= 4


def test_byte_paging_does_not_split_a_character(fresh_db):
    """A page boundary inside a multi-byte character must not corrupt it."""
    from memory.checkpoint_search import get_checkpoint

    cid = _install_checkpoint(fresh_db, thread_id="t1", narrative="é" * 10)

    got = get_checkpoint(fresh_db, cid, caller_thread_id="t1", offset_bytes=0, limit_bytes=3)

    assert got["narrative"] == "é"


def test_continuity_tolerates_any_json_type(fresh_db):
    """A tolerant reader must not raise on valid JSON of an odd shape."""
    from memory.checkpoint_search import get_checkpoint

    for payload in ("null", '"a string"', "42", "true"):
        cid = _install_checkpoint(
            fresh_db, thread_id="t1", narrative="Body.",
            checkpoint_id=f"cp-{payload[:4]}",
        )
        fresh_db.execute(
            "UPDATE checkpoints SET continuity_snapshot_json=? WHERE id=?",
            (payload, cid),
        )
        fresh_db.commit()

        got = get_checkpoint(fresh_db, cid, caller_thread_id="t1")

        assert got["narrative"] == "Body.", payload
        assert got["continuity"] == [], payload


def test_search_bounds_rows_in_sql(fresh_db):
    """Retained generations have no lifecycle bound, so the scan must not
    materialize every match before bounding it."""
    from memory.checkpoint_search import search_checkpoints

    for n in range(30):
        fresh_db.execute(
            "INSERT INTO threads (id,user_id,title,history_epoch)"
            " VALUES (?,'u','T',0)",
            (f"th{n}",),
        )
        _install_checkpoint(
            fresh_db, thread_id=f"th{n}", narrative="shared migration topic",
            checkpoint_id=f"cp-many-{n}",
        )

    hits = search_checkpoints(fresh_db, "migration", caller_thread_id="t1", limit=3)

    assert len(hits) == 3


def test_next_offset_is_a_byte_offset_the_caller_can_resume_from(fresh_db):
    """Reviewer BLOCK: the continuation range must not add characters to bytes.

    The tool told the caller where to resume by adding a Python character
    count to a byte offset, so on a non-ASCII narrative the next read would
    repeat or skip content.
    """
    from memory.checkpoint_search import get_checkpoint

    cid = _install_checkpoint(fresh_db, thread_id="t1", narrative="é" * 10)

    first = get_checkpoint(fresh_db, cid, caller_thread_id="t1", offset_bytes=0, limit_bytes=5)
    second = get_checkpoint(fresh_db, cid, caller_thread_id="t1", offset_bytes=first["next_offset_bytes"], limit_bytes=5
    )

    assert first["next_offset_bytes"] == 4, "2 chars = 4 bytes consumed"
    assert first["narrative"] + second["narrative"] == "é" * 4
    assert second["next_offset_bytes"] == 8


def test_corrupt_continuity_is_reported_not_silently_emptied(fresh_db):
    """Reviewer BLOCK: corruption must be distinguishable from an empty snapshot.

    Both used to yield [], so a corrupted retained snapshot silently degraded
    retrieval and looked identical to a generation that carried no entries.
    """
    from memory.checkpoint_search import get_checkpoint

    good = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Body.", continuity=[],
        checkpoint_id="cp-empty",
    )
    bad = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Body.", checkpoint_id="cp-corrupt",
    )
    fresh_db.execute(
        "UPDATE checkpoints SET continuity_snapshot_json='{oops' WHERE id=?",
        (bad,),
    )
    fresh_db.commit()

    empty = get_checkpoint(fresh_db, good, caller_thread_id="t1")
    corrupt = get_checkpoint(fresh_db, bad, caller_thread_id="t1")

    assert empty["continuity"] == [] and empty["continuity_unreadable"] is False
    assert corrupt["continuity"] == [] and corrupt["continuity_unreadable"] is True
    # The narrative still reads: corruption in one field does not lose the other.
    assert corrupt["narrative"] == "Body."


def test_tool_surfaces_unreadable_continuity(fresh_db):
    """A reader must be told the record is partial, not shown a clean answer."""
    import asyncio

    from tools.query_checkpoints import GetCheckpointsTool

    cid = _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Body.", checkpoint_id="cp-bad",
    )
    fresh_db.execute(
        "UPDATE checkpoints SET continuity_snapshot_json='{oops' WHERE id=?",
        (cid,),
    )
    fresh_db.commit()

    import infra.database as database
    import tools.query_checkpoints as tool_module

    async def _inline(fn):
        """Run the query on this thread: the fixture connection is thread-bound."""
        return fn()

    class _KeepOpen:
        """The tool closes what it is handed; the fixture owns this connection."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self):
            pass

    original_db, original_run = database.get_db, tool_module.run_in_db
    database.get_db = lambda: _KeepOpen(fresh_db)
    tool_module.run_in_db = _inline
    try:
        out = asyncio.run(
            GetCheckpointsTool().execute(checkpoint_id=cid, _thread_id="t1")
        )
    finally:
        database.get_db = original_db
        tool_module.run_in_db = original_run

    assert "could not be read" in out
