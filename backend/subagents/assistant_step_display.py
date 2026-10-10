"""One Assistant Step's display row: authoritative and legacy result lineage.

Split out of `subagents/child_thread.py`, which had grown past its size cap
holding both the child-thread write path and this read projection.
"""

from __future__ import annotations

import json

from core.message_display import llm_to_display
from infra.database import get_db
from infra.message_activity import DISPLAY_ROW_SELECT


def load_assistant_step_display(
    child_thread_id: str,
    assistant_step_id: str,
    *,
    db=None,
) -> dict:
    """Load one Assistant Step with authoritative and legacy result lineage."""
    _db = db or get_db()
    try:
        return _project_assistant_step(
            _lineage_rows(_db, child_thread_id, assistant_step_id), assistant_step_id,
        )
    finally:
        if not db:
            _db.close()


def assistant_step_payload(step: dict, *, message_id: str) -> dict:
    """Return the persisted Step fields shared by every runtime emitter."""
    return {
        "message_id": message_id,
        "content": step.get("content", ""),
        "metadata": step.get("metadata") or {},
        "created_at": step["created_at"],
        "seq": step.get("seq"),
    }


def _lineage_rows(_db, child_thread_id: str, assistant_step_id: str) -> list[dict]:
    """Every row one Assistant Step's display is projected from, oldest first.

    The Step itself, the tool rows it recorded, and the legacy Assistants whose
    calls those tool rows only name by id — a pre-lineage transcript split one
    Step's calls across rows, and `llm_to_display` needs them together.
    """
    assistant = _db.execute(
        DISPLAY_ROW_SELECT + "WHERE id = ? AND thread_id = ? "
        "AND role = 'assistant'",
        (assistant_step_id, child_thread_id),
    ).fetchone()
    if assistant is None:
        raise RuntimeError(
            f"missing Assistant Step {assistant_step_id!r} in {child_thread_id!r}"
        )
    assistant_row = dict(assistant)
    assistant_meta = json.loads(assistant_row.get("metadata") or "{}")
    source_calls = (
        assistant_meta.get("llm_tool_calls")
        or assistant_meta.get("tool_calls")
        or []
    )
    call_ids = {
        call["id"] for call in source_calls
        if isinstance(call, dict)
        and isinstance(call.get("id"), str)
        and call["id"]
    }
    # Separate seeks let idx_messages_tool_lineage constrain the authoritative
    # Step ID and the legacy call ID rather than scanning tool rows for one OR.
    # _ordered_rows below keeps the display's created_at,rowid order unchanged.
    children = []
    for predicate, ids in (
        ("json_extract(metadata, '$.assistant_step_id') IN ({})", {assistant_step_id}),
        (
            "json_extract(metadata, '$.assistant_step_id') IS NULL "
            "AND json_extract(metadata, '$.tool_call_id') IN ({})",
            call_ids,
        ),
    ):
        if not ids:
            continue
        children.extend(_db.execute(
            "SELECT *, rowid FROM messages WHERE thread_id = ? AND role = 'tool' "
            "AND json_valid(metadata) AND "
            + predicate.format(",".join("?" for _ in ids)),
            [child_thread_id, *sorted(ids)],
        ).fetchall())

    # The Step's own read comes last so a related copy of it cannot drop its weight.
    return _ordered_rows(
        [*_related_assistants(_db, child_thread_id, children, call_ids),
         *(dict(row) for row in children), assistant_row],
    )


def _related_assistants(
    _db, child_thread_id: str, children, call_ids: set[str],
) -> list[dict]:
    """Legacy Assistant rows whose calls these tool rows only name by id."""
    if not call_ids or not any(
        json.loads(row["metadata"] or "{}").get("assistant_step_id") is None
        for row in children
    ):
        return []
    like_terms = " OR ".join("metadata LIKE ?" for _ in call_ids)
    candidates = _db.execute(
        "SELECT *, rowid FROM messages WHERE thread_id = ? "
        "AND role = 'assistant' AND (" + like_terms + ")",
        [child_thread_id, *(f'%"{call_id}"%' for call_id in sorted(call_ids))],
    ).fetchall()
    related: list[dict] = []
    for row in candidates:
        candidate = dict(row)
        meta = json.loads(candidate.get("metadata") or "{}")
        calls = meta.get("llm_tool_calls") or meta.get("tool_calls") or []
        if any(
            isinstance(call, dict) and call.get("id") in call_ids for call in calls
        ):
            related.append(candidate)
    return related


def _ordered_rows(rows: list[dict]) -> list[dict]:
    """One row per id, oldest first, as the display projection reads them."""
    by_id = {row["id"]: row for row in rows}
    return sorted(
        by_id.values(),
        key=lambda row: (row.get("created_at", ""), row.get("rowid", 0)),
    )


def _project_assistant_step(rows: list[dict], assistant_step_id: str) -> dict:
    """The single display row `llm_to_display` projects for that Step."""
    projected = [
        row for row in llm_to_display(rows)
        if row.get("id") == assistant_step_id
    ]
    if len(projected) != 1:
        raise RuntimeError(
            f"could not project Assistant Step {assistant_step_id!r}"
        )
    step = projected[0]
    if isinstance(step.get("metadata"), str):
        step["metadata"] = json.loads(step["metadata"])
    step.pop("rowid", None)
    return step
