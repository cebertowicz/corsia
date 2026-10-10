"""The one durable row a thread's inputs and transcript are made of.

``messages`` carries both halves of a thread's record: what was said, and the
delivery state of everything queued for the model. It lives beside its indexes
(``infra.delivery_foundation_schema``) rather than inside the whole-database DDL,
for the same reason the plan and checkpoint tables do — one table's shape is
read and changed on its own.
"""

from __future__ import annotations

from infra.delivery_states import INPUT_DELIVERY_STATES, sql_state_literals

MESSAGE_SCHEMA_SQL = f"""
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES threads(id),
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        metadata TEXT,
        created_at TEXT NOT NULL,
        delivery_state TEXT NOT NULL DEFAULT 'delivered'
            CHECK (delivery_state IN ({sql_state_literals(INPUT_DELIVERY_STATES)})),
        delivery_error TEXT,
        delivery_revision INTEGER NOT NULL DEFAULT 1
            CHECK (typeof(delivery_revision) = 'integer' AND delivery_revision > 0),
        input_order INTEGER
            CHECK (input_order IS NULL OR (
                typeof(input_order) = 'integer' AND input_order > 0
            )),
        delivery_intent TEXT
            CHECK (delivery_intent IS NULL OR delivery_intent IN (
                'apply_now', 'queue_after_turn'
            )),
        target_execution_epoch INTEGER
            CHECK (target_execution_epoch IS NULL OR (
                typeof(target_execution_epoch) = 'integer'
                AND target_execution_epoch >= 0
            )),
        target_model TEXT,
        -- The condition a queued plan wake was collected for, judged again when
        -- a Turn would claim it. Outside the payload FIFO admission compares
        -- for duplicate identity, so a redelivery is still the same input.
        continuation_condition TEXT CHECK (continuation_condition IS NULL
            OR json_type(continuation_condition) = 'object'),
        delivery_options TEXT
            CHECK (delivery_options IS NULL OR (
                json_valid(delivery_options)
                AND json_type(delivery_options) = 'object'
            )),
        delivery_attempt_id TEXT,
        write_started_at TEXT,
        write_outcome TEXT
            CHECK (write_outcome IS NULL OR write_outcome IN (
                'accepted', 'proved_no_write', 'ambiguous'
            )),
        active_turn_id TEXT,
        next_attempt_at TEXT,
        seq INTEGER
    );
"""
