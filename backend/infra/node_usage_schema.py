"""Storage for each plan node's ``llm_usage`` grouped as Measurement classifies it.

``llm_usage_node_groups`` holds, per plan node, commitment and Turn, the ledger's
attributed rows grouped on exactly what Measurement's pricing groups on before it
prices anything (``core.measurement_pricing._shapes_sql``): the model selector, the
context manifest, admission, input tier and idle hours. Identity resolution and
prices are still applied at read, over these few groups instead of every call.

The tables are defined here so a fresh schema and the migration create the same
thing. The triggers that keep the groups exact, their definition and the rebuild
live in ``core.node_usage_groups``: the classification SQL they embed is
Measurement's, which ``infra`` never imports.

``llm_usage_node_state`` names the definition the triggers and rows were built
with and how far a rebuild has reached; a reader uses the groups only for nodes the
current definition has rebuilt, and reads the ledger for the rest.
"""

from __future__ import annotations

NODE_GROUPS_TABLE = "llm_usage_node_groups"

NODE_USAGE_TABLES: dict[str, str] = {
    # Token sums are of admitted rows only (others add 0): a group that is not
    # admitted is never priced, so its sums are never read. ``total_tokens`` sums
    # the integer totals, ``total_known`` counts them and ``total_odd`` counts a
    # stored total that is neither NULL nor an integer, which a reader leaves to
    # the ledger. ``last_at`` is at least the latest ``created_at`` the group has
    # held: raised on every insert, never lowered.
    #
    # The rows live in their primary key (WITHOUT ROWID), so a write touches one
    # B-tree, not a table plus a key index. A primary key column cannot be NULL
    # there, so the nullable key columns are also folded into ``group_key``:
    # ``quote()`` of each, comma-joined, which is NULL-safe, never fails on any
    # value and is unambiguous. The plain columns stay for readers.
    "llm_usage_node_groups": """
    CREATE TABLE IF NOT EXISTS llm_usage_node_groups (
        plan_node_id TEXT NOT NULL, group_key TEXT NOT NULL,
        admission TEXT NOT NULL, input_tier TEXT NOT NULL, idle_hours INTEGER NOT NULL,
        plan_commitment_id TEXT, turn_id TEXT, model TEXT, context_manifest_id TEXT,
        records INTEGER NOT NULL, prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
        cache_creation_tokens INTEGER NOT NULL, cache_read_tokens INTEGER NOT NULL,
        total_tokens INTEGER NOT NULL, total_known INTEGER NOT NULL, total_odd INTEGER NOT NULL,
        last_at TEXT NOT NULL,
        PRIMARY KEY (plan_node_id, group_key, admission, input_tier, idle_hours)
    ) WITHOUT ROWID""",
    # Commitment reads (``plans.usage_cost``, ``plans.commitment_cost``) look groups up by commitment.
    "idx_llm_usage_node_groups_commitment": """
    CREATE INDEX IF NOT EXISTS idx_llm_usage_node_groups_commitment ON llm_usage_node_groups(plan_commitment_id)""",
    # ``rebuilt_below`` is an exclusive plan_node_id bound; ``complete`` is 1 once
    # every node is rebuilt under ``definition``.
    "llm_usage_node_state": """
    CREATE TABLE IF NOT EXISTS llm_usage_node_state (
        id INTEGER PRIMARY KEY CHECK (id = 1), definition TEXT NOT NULL,
        rebuilt_below TEXT NOT NULL, complete INTEGER NOT NULL
    )""",
}

NODE_USAGE_SQL = "".join(f"{sql};\n" for sql in NODE_USAGE_TABLES.values())
