"""One change journal pulled into the existing message FIFO on activity."""

from __future__ import annotations

import json
import sqlite3

from loguru import logger

from infra.input_admission import AcceptedInput, accept_thread_input
from infra.passive_digest import build_digest, clock_event, digest_summary
from infra.passive_relevant import (  # noqa: F401
    COMMENT_CHANGE_KINDS, REVIEWER_CHANGE_KINDS, _relevant_changes, agent_roster_scope_id,
    corsia_agent_contact_scope_id,
)
from infra.queries import thread_is_available
from infra.time import utc_now_iso


def append_corsia_agent_contact_change(
    db: sqlite3.Connection, *, user_id: str, summary: str, thread_id: str | None = None,
) -> None:
    """Journal one corsia-agent contact change for a user's project agents."""
    append_change(
        db,
        object_kind="agent_roster",
        object_id=corsia_agent_contact_scope_id(user_id),
        change_kind="updated",
        summary=summary,
        details={"thread_id": thread_id} if thread_id else None,
    )


def append_thread_context_note(
    db: sqlite3.Connection,
    *,
    thread_id: str,
    summary: str,
    details: dict | None = None,
    actor_thread_id: str | None = None,
) -> int:
    """Journal one product-generic passive note for exactly one thread."""
    return append_change(
        db,
        object_kind="thread_context",
        object_id=thread_id,
        change_kind="updated",
        summary=summary,
        details=details,
        actor_thread_id=actor_thread_id,
    )


# A note whose details carry this key is journaled again when the digest that
# carried it ends without reaching the model, so a restart or a Stop before the
# write cannot lose it. A digest that may have been written is never repeated.
REOFFER_UNWRITTEN = "reoffer_unwritten"


def reoffer_unwritten_notes(db: sqlite3.Connection, message_ids: tuple[str, ...]) -> None:
    """Journal again each marked note from digests that ended proved unwritten."""
    for message_id in message_ids:
        row = db.execute(
            "SELECT thread_id,metadata FROM messages WHERE id=? AND write_outcome='proved_no_write' "
            "AND delivery_state IN ('failed','cancelled') AND json_valid(metadata) "
            "AND json_extract(metadata,'$.event_type')='context.digest'",
            (message_id,),
        ).fetchone()
        if row is None:
            continue
        for event in json.loads(row["metadata"]).get("context_events") or ():
            details = event.get("details") if isinstance(event, dict) else None
            if isinstance(details, dict) and details.get(REOFFER_UNWRITTEN):
                # Rendering lifted the chip out of the details; put it back.
                chip = {"chip": event["chip"]} if "chip" in event else {}
                append_thread_context_note(
                    db, thread_id=str(row["thread_id"]), summary=str(event["summary"]),
                    details={**chip, **details},
                )


def append_change(
    db: sqlite3.Connection,
    *,
    object_kind: str,
    object_id: str,
    change_kind: str,
    summary: str,
    created_at: str | None = None,
    details: dict | None = None,
    actor_thread_id: str | None = None,
) -> int:
    """Append one source change, independent of its subscriber count."""
    row = db.execute(
        "INSERT INTO context_change_journal "
        "(object_kind,object_id,change_kind,summary,details_json,actor_thread_id,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            object_kind,
            object_id,
            change_kind,
            summary,
            json.dumps(details, sort_keys=True) if details else None,
            actor_thread_id,
            created_at or utc_now_iso(),
        ),
    )
    assert row.lastrowid is not None
    return row.lastrowid


def _visible_agent_scope(row: sqlite3.Row | dict | None) -> tuple[str, str | None] | None:
    """Return one row's roster scope only while it contributes to the roster."""
    if row is None:
        return None
    values = dict(row)
    responsibility = str(values.get("agent_responsibility") or "").strip()
    user_id = str(values.get("user_id") or "").strip()
    if not (
        user_id
        and bool(values.get("agent_enabled"))
        and str(values.get("agent_status") or "active") == "active"
        and responsibility
        and thread_is_available(values)
    ):
        return None
    project_id = values.get("project_id")
    return user_id, str(project_id) if project_id is not None else None


def append_agent_roster_transition(
    db: sqlite3.Connection,
    *,
    before: sqlite3.Row | dict | None,
    after: sqlite3.Row | dict | None,
    summary: str,
    change_kind: str = "updated",
) -> None:
    """Journal one roster mutation to each exact scope whose map changed."""
    scopes = list(
        dict.fromkeys(
            scope
            for scope in (_visible_agent_scope(before), _visible_agent_scope(after))
            if scope is not None
        )
    )
    source = after or before
    details = {"thread_id": str(dict(source)["id"])} if source is not None else None
    for user_id, project_id in scopes:
        append_change(
            db,
            object_kind="agent_roster",
            object_id=agent_roster_scope_id(user_id, project_id),
            change_kind=change_kind,
            summary=summary,
            details=details,
        )
    workspace_candidate = any(
        row is not None and dict(row).get("project_id") is None
        for row in (before, after)
    )
    if source is not None and workspace_candidate:
        from infra.corsia_agent import get_corsia_agent
        from infra.corsia_agent import CorsiaAgentSettingError

        source_values = dict(source)
        try:
            corsia_agent_id = get_corsia_agent(db=db)
        except CorsiaAgentSettingError:
            logger.warning(
                "Skipping corsia-agent contact journal: designation setting is corrupt"
            )
            return
        if corsia_agent_id == source_values["id"]:
            user_id = str(source_values.get("user_id") or "")
            if user_id:
                append_corsia_agent_contact_change(
                    db,
                    user_id=user_id,
                    summary=summary,
                    thread_id=str(source_values["id"]),
                )


def _latest_roster_change(
    db: sqlite3.Connection,
    thread_id: str,
    user_id: str,
    project_id: str | None,
    after: int,
    through: int,
) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM context_change_journal WHERE object_kind='agent_roster' "
        "AND (object_id=? OR (object_id=? AND ? IS NOT NULL)) "
        "AND seq>? AND seq<=? "
        "AND (actor_thread_id IS NULL OR actor_thread_id<>?) "
        "ORDER BY seq DESC LIMIT 1",
        (
            agent_roster_scope_id(user_id, project_id),
            corsia_agent_contact_scope_id(user_id),
            project_id,
            after,
            through,
            thread_id,
        ),
    ).fetchone()


def _journal_head(db: sqlite3.Connection, thread_id: str) -> int:
    """The journal seq a digest reads through, once its plans' owed statuses are in.

    A plan write journals its owner's status after commit; the digest must not
    go out ahead of that, so whatever this thread's plans still owe is
    journaled first.
    """
    from plans.public import drain_status_for_digest

    drain_status_for_digest(db, thread_id)
    return int(db.execute("SELECT COALESCE(MAX(seq),0) FROM context_change_journal").fetchone()[0])


def _roster_content(db: sqlite3.Connection, thread_id: str, roster_change: sqlite3.Row | None) -> str | None:
    """The current responsibility map, when a roster change makes it part of the digest."""
    if roster_change is None:
        return None
    from core.agent_context_sources import current_responsibility_map_snapshot

    return current_responsibility_map_snapshot(thread_id, db)["content"]


def materialize_pending_digest(
    db: sqlite3.Connection, thread_id: str, *, admit_while_fenced: bool = False,
) -> AcceptedInput | None:
    """Pull relevant unseen changes into one passive FIFO input."""
    if thread_id == "ui":  # Reserved user principal, never a runnable thread.
        return None
    session = db.execute(
        "SELECT user_id,project_id,last_context_change_seq FROM threads WHERE id=?",
        (thread_id,),
    ).fetchone()
    if session is None:
        raise LookupError(f"thread does not exist: {thread_id}")
    after = int(session["last_context_change_seq"])
    through = _journal_head(db, thread_id)
    observed_at = utc_now_iso()
    clock = clock_event(db, thread_id, observed_at)
    from infra.reviewer_watch import watched_reviews

    rows, overflow = _relevant_changes(
        db,
        thread_id=thread_id,
        user_id=str(session["user_id"]),
        project_id=session["project_id"],
        after=after,
        through=through,
        watched=watched_reviews(db, thread_id),
    )
    roster_change = _latest_roster_change(
        db, thread_id, str(session["user_id"]), session["project_id"], after, through)
    accepted = None
    if rows or clock is not None:
        roster_content = _roster_content(db, thread_id, roster_change)
        content, events = build_digest(
            rows,
            overflow=overflow,
            clock=clock,
            roster_change=roster_change,
            roster_content=roster_content,
        )
        clock_suffix = f":{clock['created_at']}" if clock is not None else ""
        accepted = accept_thread_input(
            thread_id,
            role="system",
            content=content,
            intent="queue_after_turn",
            message_id=(
                f"context-digest:{thread_id}:{after + 1}:{through}{clock_suffix}"
            ),
            metadata={
                "type": "context_event",
                "event_type": "context.digest",
                "event_disposition": "passive",
                "summary": digest_summary(content, events),
                "context_events": events,
                "journal_from_seq": after + 1,
                "journal_through_seq": through,
                "detail_truncated": overflow,
                **(
                    {"context_clock_at": clock["created_at"]}
                    if clock is not None
                    else {}
                ),
            },
            delivery_options={"native_mode": "event", "attachments": []},
            created_at=(
                observed_at if clock is not None else str(rows[0]["created_at"])
            ),
            admit_while_fenced=admit_while_fenced,
            db=db,
        )
    db.execute(
        "UPDATE threads SET last_context_change_seq=? WHERE id=?",
        (through, thread_id),
    )
    return accepted
