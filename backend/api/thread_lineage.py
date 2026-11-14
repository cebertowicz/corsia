"""Tool-lineage fetch for the session-messages pager: result children of the selected anchors."""

from __future__ import annotations

import json

from infra.id_bind_chunk import id_chunks


def _parse_metadata(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _assistant_call_ids(row: dict) -> set[str]:
    metadata = _parse_metadata(row.get("metadata"))
    calls = metadata.get("llm_tool_calls") or metadata.get("tool_calls") or []
    return {
        call["id"]
        for call in calls
        if isinstance(call, dict)
        and isinstance(call.get("id"), str)
        and call["id"]
    }


def _fetch_lineage_rows(db, thread_id: str, anchors: list[dict]) -> list[dict]:
    """Fetch every result child that can resolve to the selected anchors.

    New children are selected by authoritative Assistant-Step ID. Legacy
    children without an Assistant-Step ID are selected by tool-call ID, then
    resolved against assistant declarations in this thread so a reused legacy
    ID cannot attach to the wrong page.
    """
    assistant_anchors = [row for row in anchors if row.get("role") == "assistant"]
    step_ids = {
        row["id"] for row in assistant_anchors
        if isinstance(row.get("id"), str) and row["id"]
    }
    call_ids = set().union(*(_assistant_call_ids(row) for row in assistant_anchors)) \
        if assistant_anchors else set()
    if not step_ids and not call_ids:
        return anchors

    # Two seeks on idx_messages_tool_lineage rather than one OR, which scans
    # every tool row and reads its content to reach metadata. A child naming
    # another step never resolves here, so only unnamed children match by
    # call ID. Final order is applied below, so neither query sorts, and each
    # id set is sought in chunks so one statement binds a page of any size.
    child_rows: list[dict] = []
    for predicate, ids in (
        ("json_extract(metadata, '$.assistant_step_id') IN ({})", step_ids),
        (
            "json_extract(metadata, '$.assistant_step_id') IS NULL "
            "AND json_extract(metadata, '$.tool_call_id') IN ({})",
            call_ids,
        ),
    ):
        if not ids:
            continue
        for chunk in id_chunks(sorted(ids)):
            child_rows.extend(dict(row) for row in db.execute(
                "SELECT *, rowid FROM messages WHERE thread_id = ? AND role = 'tool' "
                "AND json_valid(metadata) AND "
                + predicate.format(",".join("?" for _ in chunk)),
                [thread_id, *chunk],
            ).fetchall())

    relevant_assistants: list[dict] = []
    legacy_children = [
        row for row in child_rows
        if _parse_metadata(row.get("metadata")).get("assistant_step_id") is None
    ]
    if legacy_children and call_ids:
        # Legacy rows have no parent ID. Fetch only assistant rows whose JSON
        # may contain one of the relevant IDs, then make the exact decision in
        # the shared lineage resolver rather than trusting string matching.
        for chunk in id_chunks(sorted(call_ids)):
            like_terms = " OR ".join("metadata LIKE ?" for _ in chunk)
            candidates = db.execute(
                "SELECT *, rowid FROM messages WHERE thread_id = ? "
                "AND role = 'assistant' AND (" + like_terms + ")",
                [thread_id, *(f'%"{call_id}"%' for call_id in chunk)],
            ).fetchall()
            relevant_assistants.extend(
                dict(row) for row in candidates
                if _assistant_call_ids(dict(row)) & call_ids
            )

    by_id = {
        row["id"]: row
        for row in [*relevant_assistants, *anchors, *child_rows]
        if isinstance(row.get("id"), str)
    }
    return sorted(
        by_id.values(), key=lambda row: (row.get("created_at", ""), row.get("rowid", 0)),
    )
