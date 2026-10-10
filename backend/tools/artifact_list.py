"""Read-only artifact listing for shared-document ownership and freshness."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from infra.auth import may_access_artifact
from infra.database import get_db
from infra.db_async import run_in_db
from registry.doc_freshness import freshness
from registry.identity_bridge import OWNER_REGISTRATION_STATES
from registry.identity_metadata import parse_metadata
from registry.shared_principals import (
    acting_owner,
    is_under_shared_roots,
    owner_label,
    shared_roots,
)
from tools.base import Tool
from tools.delegation import _require_context, _tool_schema
from tools.artifact_tool_helpers import to_json

_PAGE_SIZE = 50
_STATES = {"overdue", "never_verified"}


def _shared_document_rows(
    db,
    *,
    actor_thread_id: str,
    user_id: str,
    owned: bool,
    state: str | None,
) -> list[dict[str, Any]]:
    """Return every accessible shared document row matching the filters."""
    owner_states = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    rows = db.execute(
        f"""SELECT i.id, i.title, i.path, i.external_id, i.metadata,
                   r.parent_thread_id, i.updated_at
            FROM artifact_identities i
            JOIN artifact_registrations r ON r.artifact_id=i.id
            WHERE i.state='active'
              AND i.type='document'
              AND r.relationship_state IN ({owner_states})
            ORDER BY i.updated_at DESC, i.id ASC""",
        OWNER_REGISTRATION_STATES,
    ).fetchall()
    owner_principal = acting_owner(actor_thread_id, False, db=db)
    now = datetime.now(timezone.utc)
    from infra import config

    # Resolve roots once, from the selected workspace root like every other
    # shared-root caller, and each owner's access and label once.
    roots = shared_roots(workspace=config.WORKSPACE, db=db)
    access: dict[str, bool] = {}
    labels: dict[str, str] = {}
    results = []
    for raw in rows:
        row = dict(raw)
        owner = row["parent_thread_id"]
        if owned and owner != owner_principal:
            continue
        if owner not in access:
            access[owner] = may_access_artifact(owner, user_id, db=db)
        if not access[owner]:
            continue
        path = row["path"] or row["external_id"]
        if not path or not is_under_shared_roots(path, roots):
            continue
        metadata = parse_metadata(row["metadata"])
        projection = freshness(path, metadata, now)
        if state is not None and projection["state"] != state:
            continue
        if owner not in labels:
            labels[owner] = owner_label(owner, db=db)
        results.append({
            "artifact_id": row["id"],
            "path": path,
            "display_name": row["title"] or Path(path).name,
            "owner_name": labels[owner],
            "update_trigger": metadata.get("update_trigger"),
            "freshness": projection,
        })
    return results


class ArtifactListTool(Tool):
    """List accessible registered shared documents with freshness state."""

    def __init__(self, workspace: Path | None = None):
        # Accepted for the uniform tool constructor. A project thread's tool
        # workspace is its project root, which cannot resolve project roots.
        del workspace

    @property
    def name(self) -> str:
        return "artifact_list"

    @property
    def description(self) -> str:
        return (
            "List registered shared documents with owner, full update trigger, and "
            "freshness. Set owned=true for documents owned by the current agent or "
            "its persistent parent. Returns 50 rows per page. An owner clears a "
            "never_verified or overdue row with artifact_update mark_verified=true."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            "owned": {"type": "boolean"},
            "state": {
                "type": ["string", "null"],
                "enum": ["overdue", "never_verified", None],
            },
            "cursor": {"type": ["string", "null"]},
        }, ["owned"])

    async def execute(
        self,
        owned: bool,
        state: str | None = None,
        cursor: str | None = None,
        **kwargs: Any,
    ) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        actor_thread_id, actor = context
        if state is not None and state not in _STATES:
            return "Error: state must be `overdue`, `never_verified`, or null."
        try:
            offset = int(cursor or "0")
            if offset < 0 or str(offset) != (cursor or "0"):
                raise ValueError
        except ValueError:
            return "Error: cursor must be the non-negative cursor returned by artifact_list."

        def _list() -> dict[str, Any]:
            db = get_db()
            try:
                results = _shared_document_rows(
                    db,
                    actor_thread_id=actor_thread_id,
                    user_id=actor["user_id"],
                    owned=owned,
                    state=state,
                )
                page = results[offset: offset + _PAGE_SIZE]
                next_offset = offset + len(page)
                return {
                    "rows": page,
                    "next_cursor": str(next_offset) if next_offset < len(results) else None,
                    "total": len(results),
                }
            finally:
                db.close()

        try:
            return to_json(await run_in_db(_list))
        except Exception as exc:
            return f"Error listing artifacts: {exc}"
