"""Checkpoint search runs through the index, and matches by token.

Two contracts worth pinning separately from the retrieval behaviour tests.
The first is why the index exists at all: if search stopped consulting it,
every checkpoint write would still pay to maintain it while lookups went back
to scanning, and nothing else in the suite would notice.

The second is the behavioural cost of that change, recorded deliberately
rather than discovered later: an index tokenizes, so matching is no longer
substring matching.
"""

from __future__ import annotations

from infra.thread_confinement import Reader

from checkpoint_retrieval_fixtures import (  # noqa: F401
    _install_checkpoint,
    fresh_db,
)


def test_search_is_served_by_the_index_not_a_scan(fresh_db):
    """The query plan must consult checkpoints_fts.

    Asserted through the plan rather than through results, because a scan and
    an index lookup return the same rows — which is exactly why a silent
    regression here would otherwise be invisible.
    """
    from infra.db_fts import fts5_escape

    plan = fresh_db.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT c.id FROM checkpoints_fts "
        "JOIN checkpoints c ON c.generation_order = checkpoints_fts.rowid "
        "WHERE checkpoints_fts MATCH ?",
        (fts5_escape("routing"),),
    ).fetchall()

    detail = " ".join(str(row[3]) for row in plan)
    # The MATCH constraint is pushed into the index rather than evaluated per
    # row, and checkpoints is then reached by rowid instead of scanned.
    assert "checkpoints_fts VIRTUAL TABLE INDEX" in detail
    assert "SEARCH c USING INTEGER PRIMARY KEY" in detail


def test_a_quote_in_the_query_does_not_break_the_search(fresh_db):
    """A query containing a double quote returns results instead of raising.

    FTS5 reads a bare quote as the end of a quoted term, so an unescaped one
    builds an unterminated expression and `sqlite3.OperationalError` reaches
    the caller. Message search hid this behind a LIKE fallback; checkpoint
    search has no fallback by design, so the escaping has to be correct.
    """
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db, thread_id="t1",
        narrative="We rejected the polling design and chose event routing.",
    )

    assert search_checkpoints(fresh_db, 'polling"', reader=Reader.agent("t1")) != []
    assert search_checkpoints(fresh_db, '"', reader=Reader.agent("t1")) == []
    assert search_checkpoints(fresh_db, 'a"b', reader=Reader.agent("t1")) == []


def test_word_prefix_recall_is_preserved(fresh_db):
    """A word fragment still finds the word, as the substring scan did.

    This is the recall the index had to keep. Prefix matching is what makes
    it work without going back to scanning every retained generation.
    """
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db, thread_id="t1",
        narrative="We rejected the polling design and chose event routing.",
    )

    assert search_checkpoints(fresh_db, "poll", reader=Reader.agent("t1")) != []
    assert search_checkpoints(fresh_db, "rout", reader=Reader.agent("t1")) != []
    assert search_checkpoints(fresh_db, "polling", reader=Reader.agent("t1")) != []


def test_matching_is_anchored_at_word_starts(fresh_db):
    """A fragment from inside a word does not match, and that is deliberate.

    The one recall difference from the substring scan this replaced. Infix
    matching is what an index cannot serve without scanning, and searching
    prose for the middle of a word is rarely the intent. Pinned so the
    boundary is a contract rather than a surprise.
    """
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Extrapolling is a different word.",
    )

    assert search_checkpoints(fresh_db, "extra", reader=Reader.agent("t1")) != []
    assert search_checkpoints(fresh_db, "polling", reader=Reader.agent("t1")) == []


def test_hyphenated_terms_still_match(fresh_db):
    """Punctuated terms survive tokenization because they are matched as phrases.

    The failure this guards against is assuming an index breaks anything with
    a hyphen in it: both sides tokenize identically, so the phrase lines up.
    """
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db, thread_id="t1", narrative="Routine progress.",
        continuity=[{"statement": "Region pinned to eu-west-1 for latency."}],
    )

    assert search_checkpoints(fresh_db, "eu-west-1", reader=Reader.agent("t1")) != []


def test_snake_case_terms_match_unicode61_token_boundaries(fresh_db):
    """An underscore separates tokens for both FTS and entry evidence matching."""
    from memory.checkpoint_search import search_checkpoints

    _install_checkpoint(
        fresh_db,
        thread_id="t1",
        narrative="Routine.",
        continuity=[{"statement": "The agent_search route remains enabled."}],
    )

    assert search_checkpoints(fresh_db, "agent_search", reader=Reader.agent("t1")) != []


def test_entry_index_uses_the_shared_snapshot_search_text(fresh_db):
    """The derived index carries the same compact body text every reader defines."""
    from core.continuity_render import read_continuity_snapshot

    _install_checkpoint(
        fresh_db,
        thread_id="t1",
        narrative="Routine.",
        continuity=[{"statement": "Shared text remains canonical."}],
    )

    snapshot = fresh_db.execute(
        "SELECT continuity_snapshot_json FROM checkpoints"
    ).fetchone()[0]
    expected = read_continuity_snapshot(snapshot)[0].search_text
    actual = fresh_db.execute(
        "SELECT search_text FROM checkpoint_entry_search"
    ).fetchone()[0]

    assert actual == expected


def test_json_key_names_are_not_searchable_terms(fresh_db):
    """Entry keys such as ``statement`` are structure, not words to match."""
    from memory.checkpoint_search import _entry_rows

    for index in range(3):
        _install_checkpoint(
            fresh_db,
            thread_id="t1",
            narrative="Routine.",
            continuity=[{
                "entry_id": index + 1,
                "field": "facts",
                "body": {
                    "statement": f"Indexed word {index}.",
                    "source_refs": [{"kind": "message", "reference": "m-1"}],
                },
                "revision": 1,
            }],
            checkpoint_id=f"keys-{index}",
        )

    for structural in ("statement", "source_refs", "kind"):
        assert _entry_rows(fresh_db, structural, "u", None, 50, 25, Reader.agent("t1")) == []
    assert len(_entry_rows(fresh_db, "indexed", "u", None, 50, 25, Reader.agent("t1"))) == 3


def test_index_stays_in_sync_when_a_narrative_is_written(fresh_db):
    """Capture fills raw_result after the row exists; the index must follow.

    A checkpoint row is inserted empty and updated at capture, so an
    insert-only index would never see any narrative at all.
    """
    from memory.checkpoint_search import search_checkpoints

    fresh_db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,"
        " mode,status,source_harness,source_model,writer_harness,writer_model,"
        " continuity_base_revision,continuity_base_snapshot_json,started_at) "
        "VALUES ('cp-late','t1',0,100,'m-100','pressure','cold','running',"
        " 'native','m','native','m',0,'[]','2026-09-01T00:00:00')"
    )
    fresh_db.commit()
    assert search_checkpoints(fresh_db, "afterwards", reader=Reader.agent("t1")) == []

    fresh_db.execute(
        "UPDATE checkpoints SET status='installed', raw_result=?,"
        " continuity_snapshot_json='[]', installed_at='2026-09-01T00:05:00'"
        " WHERE id='cp-late'",
        ("The narrative arrives afterwards, at capture.",),
    )
    fresh_db.commit()

    hits = search_checkpoints(fresh_db, "afterwards", reader=Reader.agent("t1"))
    assert [h["checkpoint_id"] for h in hits] == ["cp-late"]
