"""A parent stops the temporary workers it spawned, through the existing Stop path."""
from __future__ import annotations

from typing import Any

from infra.db_async import run_in_db
from tools.base import Tool


def _stop_plan(caller: str, target: str, user_id: str) -> tuple[list[str], list[str], str | None, str | None]:
    """(threads to stop, standing agents skipped, parent id, refusal), read in one connection."""
    from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator
    from infra.database import get_db
    from infra.open_time import thread_and_descendants
    from infra.queries import get_thread_for_user

    db = get_db()
    try:
        if not get_thread_for_user(target, user_id, db=db):
            return [], [], None, f"Error: Thread not found: {target}"
        row = db.execute("SELECT spawned_by, agent_enabled FROM threads WHERE id=?", (target,)).fetchone()
        parent = row["spawned_by"] if row else None
        if row is None or parent is None or row["agent_enabled"]:
            return [], [], None, f"Error: {target} is not a temporary worker; only the person stops it."
        if target == caller:
            return [], [], None, "Error: a thread cannot stop itself with this tool."
        if not _spawned_under(db, target, caller) and caller != get_scope_coordinator(WORKSPACE_SCOPE, db=db):
            return [], [], None, (
                f"Error: only {parent} (the thread that spawned {target}), its spawners above it up to the "
                "nearest standing agent, or the CORSIA agent can stop it."
            )
        members = thread_and_descendants(db, target)
        info = {
            str(r["id"]): r for r in db.execute(
                f"SELECT id, spawned_by, agent_enabled FROM threads WHERE id IN ({','.join('?' * len(members))})",
                members,
            )
        }
        stop: list[str] = []
        skipped: list[str] = []
        for sid in members:
            # A standing agent, and everything beneath it, belongs to that agent.
            node, standing = sid, None
            while node in info and node != target:
                if info[node]["agent_enabled"]:
                    standing = node
                    break
                node = info[node]["spawned_by"]
            if standing is None:
                stop.append(sid)
            elif standing == sid:
                skipped.append(sid)
        return stop, skipped, str(parent), None
    finally:
        db.close()


def _spawned_under(db, target: str, caller: str) -> bool:
    """Whether ``caller`` spawned ``target`` with no standing agent between them.

    A standing agent's subtree is its own, so the climb from ``target`` stops there.
    """
    seen: set[str] = set()
    row = db.execute("SELECT spawned_by FROM threads WHERE id=?", (target,)).fetchone()
    node = row["spawned_by"] if row else None
    while node and node not in seen:
        if node == caller:
            return True
        seen.add(node)
        row = db.execute("SELECT spawned_by, agent_enabled FROM threads WHERE id=?", (node,)).fetchone()
        if row is None or row["agent_enabled"]:
            return False
        node = row["spawned_by"]
    return False


def _retire(sid: str, why: str | None, notify_parent: bool) -> tuple[bool, int, bool]:
    """Archive first (nothing queued can start, no send is admitted), then cancel wakes.

    Returns (archived now, queued inputs held, an open step still delegates to it).
    """
    from automation import schedules, watches
    from automation.plan_liveness import cancel_plan_wake
    from core.thread_lifecycle import archive_stopped_worker
    from infra.database import write_transaction
    from infra.thread_liveness import read_thread_liveness
    from plans import public as plans

    with write_transaction(None, scope="delegate_stop") as connection:
        archived = archive_stopped_worker(connection, sid, why=why)
        cancel_plan_wake(sid, db=connection)
        for watch in connection.execute(
            "SELECT id FROM expected_event_watches WHERE owner_thread_id=? AND status='armed'", (sid,),
        ).fetchall():
            watches.resolve_watch(watch["id"], db=connection)
        schedules.retire_for_thread(sid, _now_iso(), db=connection)
        step_open = plans.has_open_structured_attachment(sid, db=connection)
        if archived and notify_parent:
            stamp = connection.execute("SELECT archived_at FROM threads WHERE id=?", (sid,)).fetchone()[0]
            plans.on_child_thread_archived(connection, sid, archived_at=str(stamp))
        held = read_thread_liveness(sid, turn_active=False, db=connection).queued_inputs
    return archived, held, step_open


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _running_turn(sid: str) -> str | None:
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT id FROM active_turns WHERE thread_id=? AND status='running' "
            "ORDER BY turn_order DESC LIMIT 1", (sid,),
        ).fetchone()
    finally:
        db.close()
    return None if row is None else str(row["id"])


def _record_stop(caller: str, workers: int, mid_turn: int, not_stopped: int, skipped: int, own: bool) -> None:
    """One ``delegate_stop.stopped`` event per call (docs/observability.md)."""
    from infra.product_events import emit_product_event

    emit_product_event("delegate_stop", "stopped", thread_id=caller, properties={
        "workers": workers, "mid_turn": mid_turn, "not_stopped": not_stopped,
        "standing_skipped": skipped, "by_spawner": own,
    })


class DelegateStopTool(Tool):
    """Stop a worker the caller spawned, and every worker beneath it."""

    @property
    def name(self) -> str:
        return "delegate_stop"

    @property
    def description(self) -> str:
        return (
            "Stop a temporary worker you spawned (or one beneath it), and every worker beneath it: "
            "its running turn ends and it is archived, so queued messages stay held and nothing "
            "new starts. Allowed for the thread that spawned it, any spawner above it up to the "
            "nearest standing agent, and the CORSIA agent; a standing agent is never stopped this "
            "way. Attaching or replacing a step onto the same worker brings it back."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        from tools.delegation import _tool_schema

        return _tool_schema({
            "thread_id": {"type": "string", "description": "The worker to stop"},
            "reason": {"type": ["string", "null"], "description": (
                "Why you stopped it; recorded on any decision a stopped worker had open, which is withdrawn")},
        }, ["thread_id"])

    async def execute(self, thread_id: str, reason: str | None = None, **kwargs: Any) -> str:
        from tools import delegation
        from ws.stop_control import stop_turn_for_cause

        context = delegation._require_context(kwargs)
        if isinstance(context, str):
            return context
        caller, current = context
        target = thread_id.strip()
        stop, skipped, parent, refusal = await run_in_db(_stop_plan, caller, target, current["user_id"])
        if refusal:
            return refusal
        lines: list[str] = []
        step_open = False
        mid_turn = not_stopped = 0
        for sid in stop:
            archived, held, open_step = await run_in_db(
                _retire, sid, (reason or "").strip() or None, sid == target and caller != parent)
            step_open = step_open or (sid == target and open_step)
            turn_id = await run_in_db(_running_turn, sid)
            if turn_id is None:
                what = "was idle" if archived else "was idle and already archived"
            else:
                outcome = (await stop_turn_for_cause(sid, turn_id, plan_limit=False)).outcome
                mid_turn += 1
                not_stopped += outcome != "stopped"
                what = "stopped mid-turn" if outcome == "stopped" else f"could not stop (stop outcome: {outcome})"
            lines.append(f"{sid}: {what}; {held} queued input(s) left held")
        _record_stop(caller, len(stop), mid_turn, not_stopped, len(skipped), caller == parent)
        for sid in skipped:
            lines.append(f"{sid}: skipped, a standing agent beneath {target}; only the person stops it")
        if caller == parent:
            if step_open:
                lines.append(
                    f"A step of yours that delegates to {target} is still open and no return will come: "
                    "close it or re-delegate it.")
        elif parent:
            lines.append(f"{parent} (its spawner) was told it was stopped.")
        return "\n".join(lines)
