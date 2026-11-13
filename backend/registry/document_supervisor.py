"""Who owns a registered document, who supervises it, and who may be picked.

The one place that decides the supervisor: the document header shows it, and
comment delivery routes to it when no agent owns the document.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infra.auth import is_owner_sentinel
from infra.queries import thread_is_available
from registry.identity_metadata import parse_metadata
from registry.shared_principals import UI_PRINCIPAL, artifact_owner, document_scope

SUPERVISOR_KEY = "supervisor_thread_id"
TAKEN_OVER_KEY = "owner_user_id"


def _thread(db, thread_id: str | None):
    if not thread_id:
        return None
    return db.execute(
        "SELECT id,title,project_id,user_id,agent_enabled,lifecycle_state,archived_reason FROM threads WHERE id=?",
        (thread_id,),
    ).fetchone()


def _paused_threads(db, thread_ids: list[str]) -> set[str]:
    """The threads among ``thread_ids`` with a paused plan, in one query."""
    if not thread_ids:
        return set()
    marks = ",".join("?" * len(thread_ids))
    return {
        str(row[0]) for row in db.execute(
            f"SELECT DISTINCT owner_id FROM plans WHERE owner_kind='thread' AND state='paused' AND owner_id IN ({marks})",
            thread_ids,
        ).fetchall()
    }


def _status(row, paused: set[str]) -> str:
    """``ready``, ``paused`` or ``gone`` (archived, purged or missing)."""
    if row is None or not thread_is_available(row):
        return "gone"
    return "paused" if str(row["id"]) in paused else "ready"


def owner_kind(owner: str | None, metadata: dict[str, Any]) -> str:
    """``person`` only when the person took it over; a fallback ``ui`` or ``project:`` owner is ``none``."""
    if owner == UI_PRINCIPAL and metadata.get(TAKEN_OVER_KEY):
        return "person"
    if owner is None or is_owner_sentinel(owner):
        return "none"
    return "agent"


def _conductor(db, artifact: dict[str, Any], workspace: Path) -> str | None:
    path = artifact.get("path") or artifact.get("external_id")
    if not path:
        return None
    try:
        return document_scope(path, workspace=workspace, db=db)[1]
    except (ValueError, OSError):
        return None


def _supervisor_candidate(
    owner: str | None, kind: str, metadata: dict[str, Any], conductor: str | None,
) -> tuple[str | None, str]:
    """The supervisor the person picked, else the owning agent, else the scope conductor."""
    stored = metadata.get(SUPERVISOR_KEY)
    if stored:
        return str(stored), "picked"
    if kind == "agent":
        return owner, "owner"
    return conductor, "conductor"


def comment_recipient(db, artifact: dict[str, Any], *, workspace: Path) -> str:
    """The agent that receives a document's comments and suggestions.

    The owning agent when an agent owns it. Otherwise (the person took it over,
    or no agent owns it) the supervisor the header shows, while that agent is
    available; without one, the stored owner principal, as before.
    """
    owner = str(artifact.get("parent_thread_id") or "")
    metadata = parse_metadata(artifact.get("metadata"))
    kind = owner_kind(owner, metadata)
    if kind == "agent":
        return owner
    candidate, _source = _supervisor_candidate(owner, kind, metadata, _conductor(db, artifact, workspace))
    row = _thread(db, candidate)
    return str(row["id"]) if row is not None and thread_is_available(row) else owner


def is_multi_person(db) -> bool:
    """More than one person account uses this CORSIA.

    The one home for this check: the ownership PUT refuses a person taking a
    document over on exactly this condition, and the ownership payload reports
    it so the control never offers "You" an action that would be refused.
    """
    return person_accounts(db) > 1


def ownership(db, artifact: dict[str, Any], *, user_id: str, workspace: Path) -> dict[str, Any]:
    """The owner, the effective supervisor and the picker's choices for one document."""
    metadata = parse_metadata(artifact.get("metadata"))
    owner = artifact_owner(str(artifact["id"]), db=db)
    kind = owner_kind(owner, metadata)
    conductor = _conductor(db, artifact, workspace)
    owner_row = _thread(db, owner) if kind == "agent" else None
    scope_row = _thread(db, conductor) or owner_row
    project_id = scope_row["project_id"] if scope_row is not None else None
    candidate, source = _supervisor_candidate(owner, kind, metadata, conductor)
    row = _thread(db, candidate)
    agents = [
        agent for agent in db.execute(
            f"""SELECT id,title,project_id,user_id,agent_enabled,lifecycle_state,archived_reason FROM threads
                WHERE user_id=? AND COALESCE(agent_enabled,0)=1 AND {'project_id IS NULL' if project_id is None else 'project_id=?'}
                ORDER BY title COLLATE NOCASE""",
            (user_id,) if project_id is None else (user_id, project_id),
        ).fetchall()
        if thread_is_available(agent)
    ]
    paused = _paused_threads(db, [str(agent["id"]) for agent in agents] + ([str(row["id"])] if row is not None else []))
    status = _status(row, paused)
    supervisor = None if row is None else {
        "id": str(row["id"]), "name": str(row["title"] or row["id"]), "source": source, "status": status,
    }
    choices = [
        {"id": str(agent["id"]), "name": str(agent["title"] or agent["id"]), "status": _status(agent, paused)}
        for agent in agents
    ]
    return {
        "owner": {"kind": kind, "id": owner if kind == "agent" else None,
                  "name": str(owner_row["title"] or owner) if owner_row is not None else None},
        "supervisor": supervisor,
        "can_act": status == "ready",
        "multi_person": is_multi_person(db),
        "choices": choices,
    }


def person_accounts(db) -> int:
    return int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])
