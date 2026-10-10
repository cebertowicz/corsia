"""Bounded, blob-free reads for checkpoint status consumers."""

from __future__ import annotations


def _seed_history(thread_id: str, *, db, count: int) -> None:
    """Seed retained checkpoints with deliberately large private payloads."""
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
        "VALUES (?,?, 'openai/test-checkpoint',0,1,'t0','t0')",
        (thread_id, "user-1"),
    )
    private_payload = "x" * 32_768
    for index in range(count):
        db.execute(
            "INSERT INTO checkpoints ("
            "id,thread_id,history_epoch,previous_generation_id,through_seq,"
            "through_message_id,trigger,mode,status,source_harness,source_model,"
            "writer_harness,writer_model,originating_turn_id,first_included_seq,"
            "continuity_base_revision,continuity_base_snapshot_json,raw_result,"
            "continuity_snapshot_json,usage_json,error,started_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"checkpoint-{index}",
                thread_id,
                0,
                None,
                index + 1,
                f"message-{index}",
                "manual",
                "warm",
                "installed",
                "native",
                "openai/test-checkpoint",
                "native",
                "openai/test-checkpoint",
                None,
                None,
                0,
                private_payload,
                private_payload,
                private_payload,
                private_payload,
                None,
                f"2026-09-23T00:00:{index:02d}+00:00",
            ),
        )


def test_latest_for_thread_reads_one_summary_without_retained_payloads(isolated_db) -> None:
    """The status-specific lookup is constant-cost and blob-free."""
    from infra import checkpoint_queries
    from infra.database import get_db

    db = get_db()
    try:
        _seed_history("thread-1", db=db, count=2)
        db.commit()
        row = checkpoint_queries.latest_for_thread("thread-1", db=db)
    finally:
        db.close()

    assert row is not None and row.id == "checkpoint-1"
    assert not hasattr(row, "raw_result")


def test_model_switch_lookup_keeps_its_warm_and_cold_matching_rules(isolated_db) -> None:
    """A later unrelated row cannot hide a selected model-switch attempt."""
    from infra import checkpoint_queries
    from infra.database import get_db

    db = get_db()
    try:
        _seed_history("thread-1", db=db, count=3)
        db.execute(
            "UPDATE checkpoints SET trigger='model_switch', mode='warm', "
            "source_model='resolved-source', writer_model='resolved-source' "
            "WHERE id='checkpoint-0'"
        )
        db.execute(
            "UPDATE checkpoints SET trigger='model_switch', mode='cold', "
            "source_model='requested-source', writer_model='destination' "
            "WHERE id='checkpoint-1'"
        )
        db.execute(
            "UPDATE checkpoints SET trigger='model_switch', mode='warm', "
            "source_model='resolved-source', writer_model='other-writer' "
            "WHERE id='checkpoint-2'"
        )
        db.commit()
        warm = checkpoint_queries.model_switch_for_thread(
            "thread-1",
            source_model="resolved-source",
            cold_source_model="other-source",
            destination_model="destination",
            requested_at="2026-09-23T00:00:00+00:00",
            db=db,
        )
        cold = checkpoint_queries.model_switch_for_thread(
            "thread-1",
            source_model=None,
            cold_source_model="requested-source",
            destination_model="destination",
            requested_at="2026-09-23T00:00:00+00:00",
            db=db,
        )
    finally:
        db.close()

    assert warm is not None and warm.id == "checkpoint-0"
    assert cold is not None and cold.id == "checkpoint-1"


def test_listing_query_uses_the_thread_and_generation_index(isolated_db) -> None:
    """The order-and-limit lookup does not scan all retained generations."""
    from infra.checkpoint_queries import _SUMMARY_COLUMNS
    from infra.database import get_db

    db = get_db()
    try:
        plan = db.execute(
            f"EXPLAIN QUERY PLAN SELECT {_SUMMARY_COLUMNS} FROM checkpoints "
            "WHERE thread_id=? ORDER BY generation_order DESC LIMIT 1",
            ("thread-1",),
        ).fetchall()
    finally:
        db.close()

    assert any("checkpoints_listing" in row[3] for row in plan)
