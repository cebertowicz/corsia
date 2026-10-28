"""What deleting a thread removes: the thread and every temporary subagent beneath it.

One rule for every deletion (#2647, decision 2026-09-28 "Deleting a thread
deletes everything beneath it"): the deleted thread and its temporary
descendants go together, deepest first, and a durable agent (enabled, retaining
responsibility, or carrying an inactive roster state from a legacy demotion) is
detached to a visible root with its own subtree untouched. No spawner or adoption
check applies, so after a deletion no row's ``spawned_by``
names a thread that no longer exists. Fencing and stopping the family first,
and refusing a raced one, is the caller's (``api.thread_deletion``).
"""

from __future__ import annotations

from typing import Any

_TEMPORARY_FAMILY_SQL = """
    WITH RECURSIVE family(id, depth, path) AS (
        SELECT id, 0, json_array(id) FROM threads WHERE id = ?
        UNION ALL
        SELECT child.id, family.depth + 1, json_insert(family.path, '$[#]', child.id)
        FROM threads AS child
        JOIN family ON child.spawned_by = family.id
        WHERE COALESCE(child.agent_enabled, 0) = 0
        AND TRIM(COALESCE(child.agent_responsibility, '')) = ''
        AND COALESCE(child.agent_status, 'active') = 'active'
        AND NOT EXISTS (SELECT 1 FROM json_each(family.path) WHERE value = child.id)
    )
    SELECT id FROM family ORDER BY depth DESC
"""


def thread_family_ids(db: Any, thread_id: str) -> list[str]:
    """Return the threads deleting ``thread_id`` removes, deepest first and it last."""
    return [str(row[0]) for row in db.execute(_TEMPORARY_FAMILY_SQL, (thread_id,))]


def _durable_agent_edge_ids(db: Any, family_ids: list[str]) -> list[str]:
    """Return enabled, retained, or legacy-demoted agents spawned by the family."""
    marks = ",".join("?" for _ in family_ids)
    rows = db.execute(
        f"SELECT id FROM threads WHERE spawned_by IN ({marks}) "
        "AND (COALESCE(agent_enabled, 0) <> 0 "
        "OR TRIM(COALESCE(agent_responsibility, '')) <> '' "
        "OR COALESCE(agent_status, 'active') <> 'active')",
        family_ids,
    ).fetchall()
    return [str(row[0]) for row in rows]


def _delete_one_thread_rows(
    db: Any,
    thread: Any,
    *,
    ending_thread_ids: set[str],
) -> None:
    """Delete one already-quiesced thread and its thread-scoped state.

    Delete removes at least what purge removes: the checkpoint narrative, the
    entry index derived from it, the continuity store, and the chat-comment
    answer index and read stamps, through the same helpers retention uses (#2291).
    """
    from infra.chat_comment_answers import delete_thread_chat_comment_index_rows
    from infra.checkpoint_entry_index import delete_thread_checkpoints
    from infra.continuity_store import delete_thread_continuity
    from infra.passive_changes import append_agent_roster_transition
    from infra.presence import delete_thread_presence
    from infra.thread_updates import delete_thread_updates
    from infra.thread_driver import delete_thread_turns
    from plans import public
    from registry import models as registry_models
    from registry.ownership import rehome_on_thread_end
    from thread import runtime_boundary

    thread_id = str(thread["id"])
    # No schedule outlives its owner: a script schedule would otherwise keep
    # claiming runs for a thread that no longer exists, and its row would
    # block the thread's own deletion.
    db.execute("DELETE FROM automation_schedules WHERE thread_id = ?", (thread_id,))
    # Its event rules go too, with every outcome naming them or the thread.
    thread_rules = 'SELECT id FROM event_rules WHERE created_by = ? OR "to" = \'thread:\' || ?'
    db.execute(
        f"DELETE FROM event_route_decisions WHERE thread_id = ? OR rule_id IN ({thread_rules})",
        (thread_id, thread_id, thread_id),
    )
    db.execute(f"DELETE FROM event_rules WHERE id IN ({thread_rules})", (thread_id, thread_id))
    delete_thread_checkpoints(db, thread_id)
    delete_thread_continuity(db, thread_id)
    delete_thread_chat_comment_index_rows(db, thread_id)
    delete_thread_turns(db, thread_id)
    delete_thread_presence(db, thread_id)
    public.delete_pane_view(db, thread_id)
    rehome_on_thread_end(
        db,
        thread_id,
        "deleted",
        ending_thread_ids=ending_thread_ids,
    )
    registry_models.retire_owned_temporary(thread_id, db=db)
    db.execute(
        "DELETE FROM artifact_registrations WHERE parent_thread_id = ?", (thread_id,),
    )
    runtime_boundary.bump_history_epoch(db, thread_id)
    db.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
    append_agent_roster_transition(
        db,
        before=thread,
        after=None,
        summary=f"Agent responsibility map updated: {thread['title'] or 'Untitled'} removed",
    )
    public.on_child_thread_ended(db, thread_id, reason="deleted")
    # Ending a child can settle plan work and project an update card. Remove
    # updates only after that final plan-side effect, still before the thread.
    delete_thread_updates(db, thread_id)
    db.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
    public.rehome(thread_id, db=db)


def delete_thread_family(db: Any, family_ids: list[str]) -> None:
    """Detach the family's durable agents, then delete its members in order.

    ``family_ids`` is ``thread_family_ids`` read inside the caller's write.
    """
    from automation.managed_process_events import stage_thread_close
    from core.thread_identity import repoint_spawned_by

    # The canonical family and its managed handles retire in this same write.
    stage_thread_close(db, family_ids)
    for agent_id in _durable_agent_edge_ids(db, family_ids):
        repoint_spawned_by(db, agent_id, None)
    ending_thread_ids = set(family_ids)
    for member_id in family_ids:
        member = db.execute("SELECT * FROM threads WHERE id = ?", (member_id,)).fetchone()
        if member is not None:
            _delete_one_thread_rows(
                db,
                member,
                ending_thread_ids=ending_thread_ids,
            )
