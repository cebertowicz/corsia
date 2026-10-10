"""Indexes that bind message rows to the canonical delivery owner."""

CHECKPOINT_HOLD_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_messages_checkpoint_hold
        ON messages(thread_id, target_model, target_execution_epoch)
        WHERE delivery_state='queued'
          AND write_outcome='proved_no_write'
          AND active_turn_id IS NULL
          AND write_started_at IS NULL
"""

DELIVERY_FOUNDATION_DDL = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_id_session
        ON messages(id, thread_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_messages_delivery_attempt
        ON messages(delivery_attempt_id, input_order)
        WHERE delivery_attempt_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_messages_active_turn
        ON messages(active_turn_id, input_order)
        WHERE active_turn_id IS NOT NULL
    """,
    CHECKPOINT_HOLD_INDEX_SQL,
)

DELIVERY_FOUNDATION_SCHEMA_SQL = ";\n".join(DELIVERY_FOUNDATION_DDL) + ";"
