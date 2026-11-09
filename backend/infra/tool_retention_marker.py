"""The one durable marker that says a tool payload was trimmed by retention.

The marker's text is durable content: rows written years ago carry it, the
retention sweep tests it before rewriting a row again, and three reads ask for
it in SQL. It lives in ``infra`` because ``core`` imports ``infra`` and never
the other way round, and because the SQL below has to be the same text the
retention-stub index predicate is built from.
"""

from __future__ import annotations

# Marker prefix makes the trim idempotent: trimmed rows are skipped on re-run.
TOOL_TRIM_MARKER = "[tool output trimmed"

#: SQL that tests a row's ``content`` for the marker, and the only place the
#: marker's length is written down.
#:
#: The marker reaches SQLite inside the statement text, never as a bound
#: parameter. A parameter is a variable in the expression tree, so it never
#: matches a partial index's predicate and the planner falls back to a
#: per-thread scan — the same trap recorded at ``infra/learning_schema.py``
#: ``installed_boundary_guard`` and pinned by the v96 migration test. The
#: reader's ``WHERE`` and ``idx_messages_retention_stub``'s predicate are both
#: built from this string, so they cannot drift apart.
TOOL_TRIM_CONTENT_SQL = (
    f"substr(content, 1, {len(TOOL_TRIM_MARKER)}) = "
    f"'{TOOL_TRIM_MARKER.replace(chr(39), chr(39) * 2)}'"
)
