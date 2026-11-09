"""The one durable marker that says a tool payload was trimmed by retention.

The marker's text is durable content: rows written years ago carry it. Two
shapes of SQL test it. The scoped reader's ``WHERE`` and the retention-stub
index predicate are both built from ``TOOL_TRIM_CONTENT_SQL`` below, so they
cannot drift apart; the retention sweep in ``core/thread_lifecycle_trims.py``
keeps its own predicate with the marker bound as parameters, which is right for
a write no index serves. It lives in ``infra`` because ``core`` imports
``infra`` and never the other way round, and because one string, not two call
sites, has to hold the marker.
"""

from __future__ import annotations

# Marker prefix makes the trim idempotent: trimmed rows are skipped on re-run.
TOOL_TRIM_MARKER = "[tool output trimmed"

#: SQL that tests a row's ``content`` for the marker, and the only place the
#: marker's length is written down.
#:
#: Written for the two reads that must match an index, so the marker reaches
#: SQLite inside the statement text: a parameter is a variable in the expression
#: tree, so it never matches a partial index's predicate and the planner falls
#: back to a per-thread scan — the same trap recorded at
#: ``infra/learning_schema.py`` ``installed_boundary_guard`` and pinned by the
#: v96 migration test. The retention sweep does bind the marker, and needs no
#: index to do it.
TOOL_TRIM_CONTENT_SQL = (
    f"substr(content, 1, {len(TOOL_TRIM_MARKER)}) = "
    f"'{TOOL_TRIM_MARKER.replace(chr(39), chr(39) * 2)}'"
)
