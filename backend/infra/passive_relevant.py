"""The journal rows one thread is owed on a digest, read in one bounded statement."""

from __future__ import annotations

import sqlite3

from infra.passive_digest import DETAIL_LIMIT


def agent_roster_scope_id(user_id: str, project_id: str | None) -> str:
    """Return the exact workspace or project roster subscription key."""
    scope = f"project:{project_id}" if project_id else "workspace"
    return f"user:{user_id}:{scope}"


def corsia_agent_contact_scope_id(user_id: str) -> str:
    """Return the project-agent subscription key for a CORSIA agent."""
    return f"user:{user_id}:corsia-agent-contact"


# The artifact changes a reviewer with an open request hears about; every other
# artifact change kind stays owner-only. content_edited is reviewer-only: the
# owner already hears another writer's edit through its ownership rows.
REVIEWER_CHANGE_KINDS = ("comment.created", "content_edited")

# Comment journal kinds (written only by feedback_comments.emit_comment_created). A
# registration, owner or follower, never earns one: feedback_comments.recipients()
# wakes the owner, tagged agents and agents on the thread, and nobody else is told.
COMMENT_CHANGE_KINDS = ("comment.created",)


def _relevant_changes(
    db: sqlite3.Connection,
    *,
    thread_id: str,
    user_id: str,
    project_id: str | None,
    after: int,
    through: int,
    watched: dict[str, str] | None = None,
) -> tuple[list[sqlite3.Row], bool]:
    """The journal rows this thread is owed, newest first, and whether more were cut.

    Notes addressed to the thread are read apart from the rest so a busy
    document can never push one out of the window.
    """
    from infra.reviewer_watch import watch_json

    notes = db.execute(
        "SELECT *, 0 AS review_row FROM context_change_journal AS change "
        "WHERE change.seq>? AND change.seq<=? "
        "AND (change.actor_thread_id IS NULL OR change.actor_thread_id<>?) "
        "AND change.object_kind='thread_context' AND change.object_id=? "
        "ORDER BY change.seq DESC LIMIT ?",
        (after, through, thread_id, thread_id, DETAIL_LIMIT + 1),
    ).fetchall()
    kept = notes[:DETAIL_LIMIT]
    kinds = ",".join("?" for _ in REVIEWER_CHANGE_KINDS)
    comment_kinds = ",".join("?" for _ in COMMENT_CHANGE_KINDS)
    others = db.execute(
        "SELECT change.*, change.object_kind='artifact' AND NOT EXISTS ("
        " SELECT 1 FROM artifact_registrations AS owner "
        " WHERE owner.artifact_id=change.object_id AND owner.parent_thread_id=?"
        ") AS review_row FROM context_change_journal AS change "
        "WHERE change.seq>? AND change.seq<=? "
        "AND (change.actor_thread_id IS NULL OR change.actor_thread_id<>?) "
        "AND ("
        " (change.object_kind='artifact' AND change.change_kind<>'content_edited' "
        f" AND change.change_kind NOT IN ({comment_kinds}) AND EXISTS ("
        "  SELECT 1 FROM artifact_registrations AS registration "
        "  WHERE registration.artifact_id=change.object_id "
        "  AND registration.parent_thread_id=?))"
        " OR (change.object_kind='artifact' "
        f"  AND change.change_kind IN ({kinds}) AND EXISTS ("
        "  SELECT 1 FROM json_each(?) AS watch "
        "  WHERE json_extract(watch.value,'$.id')=change.object_id "
        "  AND julianday(change.created_at)>=julianday(json_extract(watch.value,'$.since'))))"
        " OR (change.object_kind='plan' AND EXISTS ("
        "  SELECT 1 FROM plans WHERE plans.id=change.object_id "
        "  AND plans.owner_kind='thread' AND plans.owner_id=?))"
        " OR (change.object_kind='project_context' AND change.object_id=?)"
        " OR change.object_kind IN ('workspace_context', 'global_context')"
        " OR (change.object_kind='agent_roster' AND ("
        "  change.object_id=? OR (change.object_id=? AND ? IS NOT NULL)"
        " ))"
        ") ORDER BY change.seq DESC LIMIT ?",
        (
            thread_id,
            after,
            through,
            thread_id,
            *COMMENT_CHANGE_KINDS,
            thread_id,
            *REVIEWER_CHANGE_KINDS,
            watch_json(watched or {}),
            thread_id,
            project_id,
            agent_roster_scope_id(user_id, project_id),
            corsia_agent_contact_scope_id(user_id),
            project_id,
            DETAIL_LIMIT + 1,
        ),
    ).fetchall()
    room = DETAIL_LIMIT - len(kept)
    overflow = len(notes) > DETAIL_LIMIT or len(others) > room
    rows = sorted([*kept, *others[:room]], key=lambda row: row["seq"], reverse=True)
    return rows, overflow
