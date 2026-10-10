"""Continuity schema: the two durable entry tables and their index.

Split from `continuity_store.py` under the same convention as `plan_schema.py`
and `checkpoint_schema.py`, so the table declarations have one home and the
edit semantics beside them stay readable. `schema.py` composes this into the
fresh-database script.
"""

from __future__ import annotations

CONTINUITY_DDL = (
    """
    CREATE TABLE IF NOT EXISTS continuity_store (
        thread_id TEXT NOT NULL
            REFERENCES threads(id) ON DELETE CASCADE,
        history_epoch INTEGER NOT NULL,
        -- What a staged fold patch is compared against. Bumped on every write,
        -- so a fold that read revision N cannot land after someone else wrote.
        revision INTEGER NOT NULL DEFAULT 0,
        -- Ids are small integers so a model can address them in a tool call.
        next_entry_id INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (thread_id, history_epoch)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS continuity_entries (
        thread_id TEXT NOT NULL
            REFERENCES threads(id) ON DELETE CASCADE,
        history_epoch INTEGER NOT NULL,
        entry_id INTEGER NOT NULL CHECK (entry_id > 0),
        field TEXT NOT NULL,
        body TEXT NOT NULL CHECK (json_valid(body)),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        state TEXT NOT NULL DEFAULT 'active'
            CHECK (state IN ('active', 'retired')),
        -- A retirement carries why, and the evidence for why. Both or neither.
        retired_reason TEXT,
        retired_source_ref TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        -- The checkpoint placement mark (core/placement_marks.py). A mark
        -- applies to one entry revision; a later revision is unmarked again.
        placement_state TEXT CHECK (placement_state IS NULL OR placement_state IN (
            'reaffirmed','selected','written','already','proposed','stays',
            'refused'
        )),
        placement_revision INTEGER,
        placement_detail TEXT,
        -- When the checkpoint review last gave a verdict on this entry; the
        -- rotating review queue orders by it (core/placement_review.py).
        reviewed_at TEXT,
        PRIMARY KEY (thread_id, history_epoch, entry_id),
        CHECK (
            (state = 'active' AND retired_reason IS NULL)
            OR (state = 'retired' AND length(trim(retired_reason)) > 0)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_continuity_entries_active
        ON continuity_entries(thread_id, history_epoch, field, state)
    """,
)

CONTINUITY_SCHEMA_SQL = ";\n".join(CONTINUITY_DDL) + ";"

