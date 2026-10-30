"""One roster mutation path for the human API and designated conductor tool."""

from __future__ import annotations

import json
from typing import Any

from core.thread_naming import THREAD_TITLE_MAX_CHARS
from infra.conductor import get_scope_coordinator
from infra.database import write_transaction
from infra.passive_changes import append_agent_roster_transition
from infra.product_events import emit_product_event
from infra.queries import get_thread_for_user
from infra.time import utc_now_iso


ROSTER_FRAME_FIELDS = (
    "agent_enabled", "agent_responsibility", "agent_paths", "agent_domains",
    "agent_status", "agent_notes", "title",
)


class RosterChangeError(ValueError):
    """A roster change cannot be admitted."""


def require_designated_conductor_target(
    thread_id: str,
    *,
    user_id: str,
    actor_thread_id: str,
    db,
):
    """Return an in-scope target after the canonical conductor check."""
    target = get_thread_for_user(thread_id, user_id, db=db)
    if target is None:
        raise RosterChangeError("Target thread was not found for the current user")
    actor = get_thread_for_user(actor_thread_id, user_id, db=db)
    if actor is None:
        raise RosterChangeError("Current thread no longer exists")
    if actor["project_id"] != target["project_id"]:
        raise RosterChangeError("Conductor and target must share a scope")
    scope = actor["project_id"] or "workspace"
    if get_scope_coordinator(scope, db=db) != actor_thread_id:
        raise RosterChangeError("Current thread is not the designated conductor")
    return target


def _json_list(value: Any) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        items = []
    return json.dumps(items)


def _desired_fields(thread: Any, body: dict[str, Any]) -> tuple:
    """Apply a partial roster edit without silently clearing omitted fields."""
    was_enabled = bool(thread["agent_enabled"])
    # Supplying a responsibility promotes only a thread that has none yet; a
    # demoted agent keeps its text, so editing that text leaves it demoted.
    promoting = (
        "responsibility" in body and not was_enabled
        and not thread["agent_responsibility"]
    )
    reenabling = bool(body.get("agent_enabled")) and not was_enabled
    enabled = bool(body.get(
        "agent_enabled", True if promoting else thread["agent_enabled"],
    ))
    responsibility = str(
        body.get("responsibility", thread["agent_responsibility"]) or ""
    ).strip()
    if enabled and not responsibility:
        raise RosterChangeError("responsibility required for an active agent")
    active = bool(body.get(
        "active", True if promoting or reenabling else thread["agent_status"] == "active",
    ))
    paths = (
        _json_list(body["responsibility_paths"])
        if "responsibility_paths" in body else (thread["agent_paths"] or "[]")
    )
    domains = (
        _json_list(body["responsibility_domains"])
        if "responsibility_domains" in body else (thread["agent_domains"] or "[]")
    )
    notes = body.get("coordination_notes", thread["agent_notes"])
    status = (
        str(thread["agent_status"] or "active")
        if not enabled and not was_enabled and not responsibility
        else "active" if enabled and active else "inactive"
    )
    return (
        # Blank remains NULL for a never-promoted thread. Its existing active
        # status also survives a demote, so it does not gain a false Agent identity.
        1 if enabled else 0, responsibility or None,
        paths, domains, status, notes,
    )


def _desired_name(thread: Any, body: dict[str, Any]) -> str:
    """The display name (the thread title) after this edit; omitted keeps it."""
    if "name" not in body:
        return thread["title"]
    name = body["name"]
    if not isinstance(name, str) or not name.strip():
        raise RosterChangeError("name must be a non-empty string")
    name = name.strip()
    if len(name) > THREAD_TITLE_MAX_CHARS:
        raise RosterChangeError(
            f"name must be at most {THREAD_TITLE_MAX_CHARS} characters"
        )
    return name


def change_agent(
    thread_id: str,
    body: dict[str, Any],
    *,
    user_id: str,
    actor_thread_id: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Update a user's agent row, requiring a live same-scope conductor when used by a tool.

    The authorization check, target read, mutation and change event share one
    write transaction. A retry of the same desired state emits no second event.
    """
    with write_transaction(None, scope="update_agent") as db:
        thread = get_thread_for_user(thread_id, user_id, db=db)
        if thread is None:
            raise RosterChangeError("Thread not found")
        if actor_thread_id is not None:
            require_designated_conductor_target(
                thread_id,
                user_id=user_id,
                actor_thread_id=actor_thread_id,
                db=db,
            )

        desired = (*_desired_fields(thread, body), _desired_name(thread, body))
        current = (
            thread["agent_enabled"], thread["agent_responsibility"],
            thread["agent_paths"] or "[]", thread["agent_domains"] or "[]",
            thread["agent_status"], thread["agent_notes"], thread["title"],
        )
        if desired == current:
            return {"ok": True, "changed": False, "thread_id": thread_id}
        if (
            expected_updated_at is not None
            and thread["agent_roster_updated_at"] != expected_updated_at
        ):
            raise RosterChangeError(
                "Roster entry changed; refresh agent_roster_entry and retry with its current updated_at"
            )

        updated_at = utc_now_iso()
        db.execute(
            """UPDATE threads SET agent_enabled=?, agent_responsibility=?,
               agent_paths=?, agent_domains=?, agent_status=?, agent_notes=?, title=?,
               agent_roster_updated_at=?, updated_at=? WHERE id=?""",
            (*desired, updated_at, updated_at, thread_id),
        )
        updated = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        summary = f"Agent responsibility map updated: {updated['title'] or 'Untitled'}"
        if bool(thread["agent_enabled"]) and not bool(updated["agent_enabled"]):
            from automation.schedules import retire_for_thread
            from registry.ownership import rehome_on_thread_end

            rehome_on_thread_end(db, thread_id, "agent_disabled")
            schedule_count = retire_for_thread(thread_id, updated_at, db=db)
            rule_count = db.execute(
                "UPDATE event_rules SET enabled=0, updated_at=? WHERE created_by=? AND enabled=1",
                (updated_at, thread_id),
            ).rowcount
            if schedule_count or rule_count:
                summary += f"; stopped {schedule_count} schedules and {rule_count} event rules"
        append_agent_roster_transition(
            db, before=thread, after=updated, summary=summary,
        )
        row = dict(updated)
    _publish_roster_row(row)
    if row["title"] != thread["title"]:
        emit_product_event(
            "session", "renamed", thread_id=thread_id, user_id=row["user_id"],
            properties={"title_len": len(row["title"])},
        )
    return {"ok": True, "changed": True, "thread_id": thread_id}


def _publish_roster_row(row: dict[str, Any]) -> None:
    """Tell the owner's open clients the committed roster fields of one thread.

    Rides the `session_updated` frame that already patches a cached thread row,
    so promotion and demotion move it between conversations and Agents without
    a reload. Best-effort: the committed row stays authoritative.
    """
    from infra import state

    state.broadcast_event(row["id"], row["user_id"], {
        "type": "session_updated",
        "thread_id": row["id"],
        **{key: row[key] for key in ROSTER_FRAME_FIELDS},
        "updated_at": row["updated_at"],
    })
