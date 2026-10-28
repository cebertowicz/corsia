"""#2908: the lean-then-hydrate entry read returns exactly what the joined one did.

``_reference_rows`` is the pre-#2908 query, kept verbatim: it ranks on wide rows
and joins and projects every candidate's frozen snapshot. The production read
must give the same hits, in the same order, for the node-start lookup and for
the checkpoint search tool.
"""

from __future__ import annotations

import pytest

from checkpoint_retrieval_fixtures import (  # noqa: F401
    OWNER_USER,
    _install_checkpoint,
    fresh_db,
)


def _reference_rows(db, match, user_id, thread_id, candidate_limit, per_thread):
    from memory.checkpoint_entry_rows import scope_clause

    params: list[object] = [match]
    scope = scope_clause(thread_id, params)
    params.extend((user_id, max(1, per_thread) * 20, candidate_limit))
    rows = db.execute(
        "WITH scored AS ("
        "  SELECT entry.thread_id, entry.history_epoch, entry.entry_id, "
        "         entry.revision, entry.field, entry.first_generation_order, "
        "         entry.last_generation_order, entry.search_text, "
        "         CASE WHEN entry.history_epoch != thread.history_epoch THEN 1 ELSE 0 END "
        "             AS historical, canonical.field AS canonical_field, "
        "         canonical.body AS canonical_body, canonical.revision AS canonical_revision, "
        "         canonical.state AS canonical_state, "
        "         canonical.retired_reason AS canonical_retired_reason, "
        "         bm25(checkpoint_entry_search_fts) AS relevance "
        "  FROM checkpoint_entry_search_fts "
        "  JOIN checkpoint_entry_search AS entry "
        "    ON entry.rowid = checkpoint_entry_search_fts.rowid "
        "  JOIN threads AS thread ON thread.id = entry.thread_id "
        "  LEFT JOIN continuity_entries AS canonical "
        "    ON canonical.thread_id = entry.thread_id "
        "   AND canonical.history_epoch = entry.history_epoch "
        "   AND canonical.entry_id = entry.entry_id "
        "  WHERE checkpoint_entry_search_fts MATCH ? "
        f"    {scope}"
        "    AND thread.user_id = ?"
        "), revisions AS ("
        "  SELECT *, ROW_NUMBER() OVER ("
        "      PARTITION BY thread_id, history_epoch, entry_id "
        "      ORDER BY relevance ASC, last_generation_order DESC"
        "  ) AS entry_rank FROM scored"
        "), ranked AS ("
        "  SELECT *, ROW_NUMBER() OVER (PARTITION BY thread_id "
        "      ORDER BY relevance ASC, last_generation_order DESC) AS thread_rank "
        "  FROM revisions WHERE entry_rank = 1"
        "), top AS ("
        "  SELECT * FROM ranked WHERE thread_rank <= ? "
        "  ORDER BY relevance ASC, last_generation_order DESC LIMIT ?"
        ") SELECT top.*, first_checkpoint.id AS id, first_checkpoint.id AS first_id, "
        "       COALESCE(first_checkpoint.installed_at, first_checkpoint.captured_at) "
        "           AS first_generated_at, latest_checkpoint.id AS latest_id, "
        "       COALESCE(latest_checkpoint.installed_at, latest_checkpoint.captured_at) "
        "           AS latest_generated_at, first_checkpoint.through_seq, "
        "       first_checkpoint.raw_result, first_checkpoint.continuity_snapshot_json "
        "FROM top "
        "JOIN checkpoints AS first_checkpoint "
        "  ON first_checkpoint.generation_order = top.first_generation_order "
        "JOIN checkpoints AS latest_checkpoint "
        "  ON latest_checkpoint.generation_order = top.last_generation_order "
        "ORDER BY top.relevance ASC, top.last_generation_order DESC",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _entry(entry_id: int, statement: str, revision: int = 1) -> dict:
    return {
        "entry_id": entry_id, "field": "facts",
        "body": {"statement": statement}, "revision": revision,
    }


@pytest.fixture
def corpus(fresh_db):
    """Several threads and epochs; entry 1 of t1 is revised so first != latest."""
    fresh_db.execute("INSERT INTO threads (id,user_id,title,history_epoch) VALUES ('t2', ?, 'T2', 1)", (OWNER_USER,))
    fresh_db.execute("INSERT INTO threads (id,user_id,title,history_epoch) VALUES ('t3', ?, 'T3', 0)", (OWNER_USER,))
    for revision, tail in enumerate(("alpha", "beta beta", "gamma gamma gamma"), 1):
        _install_checkpoint(
            fresh_db, thread_id="t1", narrative=f"Cache work {revision}.",
            checkpoint_id=f"t1-r{revision}",
            continuity=[
                _entry(1, f"Cache invalidation {tail}", revision),
                _entry(2, "Cache eviction is bounded"),
            ],
        )
    for epoch in (0, 1):
        _install_checkpoint(
            fresh_db, thread_id="t2", narrative="Epoch work.", history_epoch=epoch,
            checkpoint_id=f"t2-e{epoch}",
            continuity=[_entry(1, f"Cache warmup epoch {epoch}"), _entry(2, "Index cache")],
        )
    _install_checkpoint(
        fresh_db, thread_id="t3", narrative="Other.", checkpoint_id="t3-a",
        continuity=[_entry(n, f"Cache note {n} for the cache index") for n in range(1, 6)],
    )
    _install_checkpoint(
        fresh_db, thread_id="t-foreign", narrative="Foreign.", checkpoint_id="tf-a",
        continuity=[_entry(1, "Cache secrets")],
    )
    fresh_db.commit()
    return fresh_db


def test_corpus_has_a_kept_row_whose_first_checkpoint_differs_from_its_latest(corpus):
    from memory.checkpoint_entry_rows import entry_rows_matching

    rows = entry_rows_matching(corpus, '"cache"*', OWNER_USER, None, 100, 3)

    assert any(row["first_id"] != row["latest_id"] for row in rows)
    assert {row["thread_id"] for row in rows} == {"t1", "t2", "t3"}
    assert any(row["historical"] for row in rows)


@pytest.mark.parametrize("match", ['"cache"*', '"cache"* OR "gamma"*', '"index"*'])
@pytest.mark.parametrize("per_thread,limit", [(3, 100), (1, 100), (2, 4)])
def test_rows_match_the_reference_query(corpus, match, per_thread, limit):
    from memory.checkpoint_entry_rows import entry_rows_matching

    for thread_id in (None, "t1"):
        assert entry_rows_matching(
            corpus, match, OWNER_USER, thread_id, limit, per_thread
        ) == _reference_rows(corpus, match, OWNER_USER, thread_id, limit, per_thread)


def test_search_tool_returns_the_reference_hits(corpus, monkeypatch):
    import memory.checkpoint_search as search

    new = search.search_checkpoints(corpus, "cache", caller_thread_id="t1", limit=10)
    monkeypatch.setattr(search, "entry_rows_matching", lambda db, match, user, thread, limit, per_thread, caller: _reference_rows(db, match, user, thread, limit, per_thread))
    old = search.search_checkpoints(corpus, "cache", caller_thread_id="t1", limit=10)

    assert new == old and new
