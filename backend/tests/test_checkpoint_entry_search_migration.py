"""Entry-level checkpoint search migrates all installed historical generations."""

from __future__ import annotations

import time

from checkpoint_retrieval_fixtures import _install_checkpoint, fresh_db


def _drop_entry_search_objects(db) -> None:
    """Leave installed checkpoints intact while recreating a pre-v34 database."""
    db.executescript("""
        DROP TRIGGER checkpoint_entry_search_fts_update;
        DROP TRIGGER checkpoint_entry_search_fts_delete;
        DROP TRIGGER checkpoint_entry_search_fts_insert;
        DROP TABLE checkpoint_entry_search_fts;
        DROP TABLE checkpoint_entry_search;
    """)


def test_entry_search_backfill_is_idempotent_and_bounded_for_400_generations(fresh_db):
    """One repeated entry revision occupies one row and preserves both pointers."""
    from infra.database_migrations import _migrate_35_to_36

    for generation in range(400):
        _install_checkpoint(
            fresh_db,
            thread_id="t1",
            narrative="Routine.",
            continuity=[{"statement": "Indexed checkpoint entry."}],
            checkpoint_id=f"cp-{generation}",
        )
    _drop_entry_search_objects(fresh_db)

    started = time.monotonic()
    _migrate_35_to_36(fresh_db)
    elapsed = time.monotonic() - started
    _migrate_35_to_36(fresh_db)

    rows = fresh_db.execute(
        "SELECT entry_id, revision, first_generation_order, last_generation_order "
        "FROM checkpoint_entry_search"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(1, 1, 1, 400)]
    assert elapsed < 1.0, f"400-generation entry-index backfill took {elapsed:.3f}s"
