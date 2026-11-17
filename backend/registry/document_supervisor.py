"""Who owns a registered document, who supervises it, and who may be picked.

The one place that decides these: the document header shows them, comment
delivery (new comments and re-opens alike) asks ``comment_recipients``, and the
ownership PUT asks ``ownership``'s choices before
changing either. Who owns it follows the registry owner alone, the same reading
``may_edit_document`` makes: a ``ui`` owner is the person's, so agents suggest;
a ``project:`` owner is also the person's.

A supervisor pick is checked once, when the person makes it: it must be one of
``ownership``'s choices. Every reader then takes the stored pick as it is while
its agent is available, so what is stored, shown and routed is one value.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from infra.auth import is_owner_sentinel
from infra.queries import thread_is_available
from registry.identity_metadata import parse_metadata
from registry.shared_principals import (
    UI_PRINCIPAL,
    artifact_owner,
    document_project,
    person_owner_label,
    pickable_agents,
    scope_conductor,
)

log = logging.getLogger(__name__)

SUPERVISOR_KEY = "supervisor_thread_id"

# Supervisor picks are stored in artifact identity metadata.
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


def owner_kind(owner: str | None) -> str:
    """Classify both person sentinels consistently for display, routing and authority."""
    if not owner:
        return "none"
    if is_owner_sentinel(owner):
        return "person"
    return "agent"


def _scope(db, artifact: dict[str, Any], workspace: Path) -> tuple[str | None, str | None] | None:
    """The ``(project id, conductor)`` of the scope that holds the document, or ``None`` when it can't be resolved."""
    path = artifact.get("path") or artifact.get("external_id")
    if not path:
        return None
    try:
        project_id = document_project(path, workspace=workspace, db=db)[1]
        return project_id, scope_conductor(project_id, db=db)
    except (ValueError, OSError):
        log.warning("Couldn't resolve the scope of document %s", artifact.get("id"), exc_info=True)
        return None


def stored_pick(artifact: dict[str, Any]) -> str | None:
    """The supervisor the person picked, as stored; every reader and the ownership PUT read this one value."""
    return parse_metadata(artifact.get("metadata")).get(SUPERVISOR_KEY) or None


def _supervisor(
    db, owner: str | None, pick: str | None, conductor: Callable[[], str | None],
) -> tuple[Any, str]:
    """The effective supervisor's thread row and where it came from, or ``(None, "none")``.

    The first available of: the agent the person picked, the owning agent, the
    scope conductor (looked up only when reached). The header shows it and
    comments go to it, so the two cannot disagree.
    """
    candidates: list[tuple[Callable[[], str | None], str]] = [
        (lambda: pick, "picked"),
        (lambda: owner if owner_kind(owner) == "agent" else None, "owner"),
        (conductor, "conductor"),
    ]
    for candidate, source in candidates:
        row = _thread(db, candidate())
        if row is not None and thread_is_available(row):
            return row, source
    return None, "none"


def comment_recipients(db, artifact: dict[str, Any], *, workspace: Path) -> tuple[str, ...]:
    """Who every comment on a document goes to, and exactly who a re-open goes to.

    The supervisor the header shows and the owner it shows, whatever its kind:
    the owning agent, or the person (``ui``) on a document they own. A released
    document with neither still reaches the agent that last owned it.
    """
    owner = artifact_owner(str(artifact["id"]), db=db)
    row, _source = _supervisor(
        db, owner, stored_pick(artifact), lambda: (_scope(db, artifact, workspace) or (None, None))[1],
    )
    recipient_owner = UI_PRINCIPAL if owner_kind(owner) == "person" else owner
    found = tuple(dict.fromkeys(str(who) for who in (row["id"] if row is not None else None, recipient_owner) if who))
    released_owner = artifact.get("parent_thread_id")
    return found or ((str(released_owner),) if owner is None and owner_kind(released_owner) == "agent" else ())


def is_multi_person(db) -> bool:
    """More than one person account uses this CORSIA.

    Read once per request to distinguish person labels.
    """
    return person_accounts(db) > 1


# Choices are constrained by document scope, not by the number of people.
def ownership(db, artifact: dict[str, Any], *, user_id: str, workspace: Path, multi_person: bool) -> dict[str, Any]:
    """The owner, the effective supervisor and the picker's choices for one document.

    The choices are the only agents the ownership PUT accepts: the document's
    own scope's ``pickable_agents``, and none while the document can't be
    changed or its scope can't be resolved.
    """
    owner = artifact_owner(str(artifact["id"]), db=db)
    kind = owner_kind(owner)
    owner_user_id = parse_metadata(artifact.get("metadata")).get("owner_user_id")
    scope = _scope(db, artifact, workspace)
    owner_row = _thread(db, owner) if kind == "agent" else None
    row, source = _supervisor(db, owner, stored_pick(artifact), lambda: scope[1] if scope is not None else None)
    agents = [] if scope is None else pickable_agents(scope[0], user_id, db=db)
    paused = _paused_threads(db, [str(agent["id"]) for agent in agents] + ([str(row["id"])] if row is not None else []))
    status = _status(row, paused)
    supervisor = None if row is None else {
        "id": str(row["id"]), "name": str(row["title"] or row["id"]), "source": source, "status": status,
    }
    choices = [
        {"id": str(agent["id"]), "name": str(agent["title"] or agent["id"]), "status": _status(agent, paused),
         "responsibility": str(agent["agent_responsibility"] or "").strip()}
        for agent in agents
    ]
    return {
        "owner": {"kind": kind, "id": owner if kind == "agent" else None,
                  "name": str(owner_row["title"] or owner) if owner_row is not None else None,
                  "label": person_owner_label(owner, db=db, multi_person=multi_person, user_id=user_id,
                                              owner_user_id=owner_user_id),
                  "recorded_viewer": owner == "ui" and bool(owner_user_id) and owner_user_id == user_id},
        "supervisor": supervisor,
        "can_act": status == "ready",
        "multi_person": multi_person,
        "can_change": True,
        "refusal": None,
        "multi_person_refusal": None,
        # None when the scope couldn't be resolved, so the picker says it couldn't load rather than "no agents".
        "scope": None if scope is None else "project" if scope[0] is not None else "workspace",
        "choices": choices,
    }


def person_accounts(db) -> int:
    return int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])
