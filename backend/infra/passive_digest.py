"""Digest rendering and clock helpers for passive context changes."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from infra.checkpoint_queries import current_installed_through_seq


DETAIL_LIMIT = 100
CLOCK_INTERVAL = timedelta(hours=2)


def _event(row: sqlite3.Row) -> dict:
    """Render one journal row as a digest event."""
    details = json.loads(row["details_json"] or "{}")
    chip = {"chip": details.pop("chip")} if "chip" in details else {}
    return {
        "id": f"context-change:{row['seq']}",
        "seq": int(row["seq"]),
        "object_kind": str(row["object_kind"]),
        "object_id": str(row["object_id"]),
        "change_kind": str(row["change_kind"]),
        "summary": str(row["summary"]),
        "created_at": str(row["created_at"]),
        **chip,
        **({"details": details} if details else {}),
    }


def _collapse_key(row: sqlite3.Row) -> tuple:
    """Key changes so repeated edits collapse without hiding distinct paths.

    An input held while its plan was paused names its row in ``message_id``,
    so each held input is its own notice. A note addressed to the thread without a
    path is its own notice too, and a reviewer's document rows collapse per actor and kind
    so a comment and an edit stay apart.
    """
    details = json.loads(row["details_json"] or "{}")
    if not isinstance(details, dict):
        details = {}
    if row["object_kind"] == "thread_context" and details.get("path") is None:
        return ("note", row["seq"])
    if "review_row" in row.keys() and row["review_row"]:
        return (row["object_id"], row["change_kind"], row["actor_thread_id"])
    return (row["object_kind"], row["object_id"], details.get("path"),
            details.get("node_id"), details.get("message_id"))


def _bullet(text: str) -> str:
    """Render one event as a bullet whose continuation lines stay inside it."""
    return "- " + text.replace("\n", "\n  ")


def digest_summary(content: str, events: list[dict]) -> str:
    """Return the line that names a digest: a lone roster event's chip, else its head."""
    lone = events[0] if len(events) == 1 else None
    if lone is not None and lone.get("object_kind") == "agent_roster" and lone.get("chip"):
        return str(lone["chip"])
    return content.splitlines()[1]


def build_digest(
    rows: list[sqlite3.Row],
    *,
    overflow: bool,
    clock: dict | None,
    roster_change: sqlite3.Row | None = None,
    roster_content: str | None = None,
) -> tuple[str, list[dict]]:
    """Format unseen journal rows into one passive context digest."""
    latest = {}
    detail_rows = rows[:DETAIL_LIMIT]
    if roster_change is not None and all(
        row["seq"] != roster_change["seq"] for row in detail_rows
    ):
        detail_rows = [*rows[: DETAIL_LIMIT - 1], roster_change]
    for row in reversed(detail_rows):
        latest[_collapse_key(row)] = row
    events = [_event(row) for row in sorted(latest.values(), key=lambda item: item["seq"])]
    if roster_content is not None:
        # The current map is the same for every roster event, so it rides once.
        roster_events = [e for e in events if e.get("object_kind") == "agent_roster"]
        for event in roster_events:
            event["chip"] = event["summary"]
        if roster_events:
            roster_events[0]["summary"] = roster_content
    if clock is not None:
        events.append(clock)
    if overflow:
        kinds = sorted(
            {event["object_kind"] for event in events if "object_kind" in event}
        )
        summary = f"More than {DETAIL_LIMIT} context changes across {', '.join(kinds)}"
    elif len(events) == 1:
        summary = events[0]["summary"]
    else:
        summary = f"{len(events)} context updates"
    lines = (
        [summary]
        if len(events) == 1
        else [summary, *(_bullet(event["summary"]) for event in events)]
    )
    return "[PASSIVE CONTEXT DIGEST]\n" + "\n".join(lines), events


def _aware(value: str) -> datetime:
    """Parse a persisted timestamp as an aware datetime."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def clock_event(
    db: sqlite3.Connection, thread_id: str, observed_at: str,
) -> dict | None:
    """Return the periodic local-time context event when the interval elapsed.

    A stamp only suppresses a repeat while the model still sees its row, so one
    at or before the installed checkpoint boundary counts as absent.
    """
    now = _aware(observed_at).astimezone(UTC)
    local_now = now.astimezone()
    folded_through = current_installed_through_seq(thread_id, db=db) or 0
    previous = db.execute(
        "SELECT json_extract(metadata,'$.context_clock_at') AS context_clock_at "
        "FROM messages WHERE thread_id=? AND input_order IS NOT NULL "
        "AND delivery_state IN ('queued','delivered','ambiguous') "
        "AND (seq IS NULL OR seq>?) "
        "AND json_valid(metadata) "
        "AND json_type(metadata,'$.context_clock_at')='text' "
        "ORDER BY input_order DESC LIMIT 1",
        (thread_id, folded_through),
    ).fetchone()
    if previous is not None:
        last = _aware(str(previous["context_clock_at"]))
        elapsed = now - last.astimezone(UTC)
        same_local_context = (
            last.date() == local_now.date()
            and last.utcoffset() == local_now.utcoffset()
            and timedelta(0) <= elapsed < CLOCK_INTERVAL
        )
        if same_local_context:
            return None
    summary = f"Current time: {local_now:%Y-%m-%d %H:%M %Z}"
    return {
        "id": f"context-clock:{observed_at}",
        "event_type": "context.clock",
        "summary": summary,
        "created_at": local_now.isoformat(),
    }
