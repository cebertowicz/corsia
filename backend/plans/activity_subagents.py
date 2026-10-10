"""Exclude hidden subagent-owned rows from Activity indexes.

A thread with ``spawned_by`` set is a hidden subagent (``api/delegation.py``:
parent presence is the single source of truth). A visible chat keeps lineage in
``parent_thread_id`` instead, so it remains eligible for Activity. The lookup is
set-wise over the scope's plan rows: one chunked query, never a query per row.
"""
from __future__ import annotations

from infra.packed_rows import fetch_all
from plans import hierarchy


def spawners(plan_rows: list, *, db) -> set[str]:
    """Return owner thread IDs in ``plan_rows`` that are hidden subagents."""
    thread_ids = sorted({
        str(plan["owner_id"]) for plan in plan_rows if plan["owner_kind"] == "thread"
    })
    hidden: set[str] = set()
    for start in range(0, len(thread_ids), hierarchy._ID_CHUNK):
        chunk = thread_ids[start:start + hierarchy._ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        for row in fetch_all(
            db,
            f"SELECT id FROM threads WHERE id IN ({marks})"
            " AND spawned_by IS NOT NULL",
            chunk,
        ):
            hidden.add(str(row["id"]))
    return hidden
