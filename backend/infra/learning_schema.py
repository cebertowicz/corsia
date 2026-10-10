"""Indexes the Learning section's reads search, with the SQL text they must carry.

The boundary read asks which messages carry a retained installed-context
snapshot — about 0.07% of a production-shaped database. Answering it from the
table reads the metadata of every message in scope, and metadata is stored after
content with enough rows spilling to overflow pages that each read walks that
content chain first.

The index is partial on exactly the predicate the query states. SQLite only lets
a query use a partial index when its own text implies that predicate, so
:func:`installed_boundary_guard` writes both spellings from one definition: an
index whose ``WHERE`` drifts from the query is an index nothing reads.

One definition serves both the fresh schema and the v95 -> v96 migration, so
fresh and migrated databases carry identical indexes. Production databases never
run ANALYZE, so the index is shaped to win the planner's default estimates; the
v96 tests pin that.
"""

from __future__ import annotations

from core.installed_context import INSTALLED_SOURCES

#: The installed-boundary index name, so the reader's plan can be pinned to it.
INSTALLED_BOUNDARY_INDEX = "idx_messages_installed_boundaries"


def installed_boundary_guard(column: str) -> str:
    """SQL for ``column`` holding the installed-sources key as raw text.

    The key has to reach SQLite inside the statement text, never as a bound
    parameter: a parameter is a variable in the expression tree, so the partial
    index predicate no longer matches and the planner falls back to a full
    per-thread scan. The readers keep their ``json_type`` check alongside it,
    because the key text also appears inside ordinary tool content.
    """
    return f"instr({column},'{INSTALLED_SOURCES}')>0"


#: Each entry is ``table -> statement``; the table key lets a migration skip a
#: table a partial database does not have.
LEARNING_INDEXES: dict[str, str] = {
    # ``(thread_id, seq, id)`` is the boundary reader's equality and order, so
    # one seek per thread returns its boundaries already ordered and no temporary
    # B-tree is built for the id tie-break. Partial on the key predicate: the
    # index stays the size of the installed boundaries, not the message table.
    "messages": f"""
    CREATE INDEX IF NOT EXISTS {INSTALLED_BOUNDARY_INDEX}
        ON messages(thread_id, seq, id)
        WHERE {installed_boundary_guard("metadata")}""",
}

LEARNING_INDEX_SQL = "".join(f"{sql};\n" for sql in LEARNING_INDEXES.values())
