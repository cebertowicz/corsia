"""Correction anchors and non-blocking lesson-placement checks.

The lesson subsystem owns its own subject anchors.  Corrections remain immutable
outcome-event facts, so a lesson written before asynchronous classification can
join the fact on a later report read without a repair job.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from core.correction_notes import noted_subjects
from core.corrections_recording import CORRECTION_EVENT, is_owner_authored_message
from core.placement_proposal import names_in, shares_run

logger = logging.getLogger(__name__)

SUBJECT_PREFIX = "message:"
CHECK_NAME = "incident-details"
CHECK_OWNER = "Self-Improvement Infrastructure"
CHECK_RETIREMENT_CONDITION = (
    "fix or remove when overrides exceed half of fires over at least a month of use"
)

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_IDENTIFIER = re.compile(r"\b(?:[A-Z]{2,}[-_]?)?\d{3,}\b")
_QUOTE = re.compile(r"[\"“”][^\"“”]{20,}[\"“”]")


def merge_subjects(*groups: Iterable[object]) -> tuple[str, ...]:
    """Keep stable, non-empty subject keys in first-seen order."""
    merged: list[str] = []
    for group in groups:
        for raw in group:
            subject = raw.strip() if isinstance(raw, str) else ""
            if subject and subject not in merged:
                merged.append(subject)
    return tuple(merged)


def current_owner_subjects(db: sqlite3.Connection, thread_id: str) -> tuple[str, ...]:
    """Return every owner-authored input consumed by this thread's live turn.

    A correction note in the turn's inputs names its correction too, so a lesson
    placed while triaging an earlier turn's correction joins that correction.
    """
    rows = db.execute(
        """
        SELECT id, role, content, metadata
        FROM messages
        WHERE thread_id=?
          AND active_turn_id=(
              SELECT id FROM active_turns WHERE thread_id=? AND status='running'
          )
        ORDER BY input_order, id
        """,
        (thread_id, thread_id),
    ).fetchall()
    return merge_subjects(
        [f"{SUBJECT_PREFIX}{row['id']}" for row in rows if is_owner_authored_message(
            role=row["role"], content=row["content"], metadata=row["metadata"],
        )],
        *(noted_subjects(row["metadata"]) for row in rows if row["role"] == "system"),
    )


def incident_detail_flags(text: str, source: str, user_names: list[str]) -> tuple[str, ...]:
    """Flag likely incident detail in a lesson body; never decide whether to write it.

    Names and repeated runs use placement's own screen, so a line is flagged here
    exactly when a proposal of it would be refused.
    """
    flags: list[str] = []
    if _DATE.search(text):
        flags.append("date")
    if _IDENTIFIER.search(text):
        flags.append("id")
    if names_in(text, source, user_names):
        flags.append("name")
    if _QUOTE.search(text) or shares_run(text, source):
        flags.append("quote")
    return tuple(flags)


def record_placement_check(
    db: sqlite3.Connection, *, thread_id: str, history_epoch: int, entry_id: int,
    revision: int, flags: Iterable[str], override_reason: str | None,
) -> None:
    """Persist each detected flag once; an override upgrades that same observation."""
    now = datetime.now(timezone.utc).isoformat()
    for flag in flags:
        db.execute(
            """
            INSERT INTO lesson_placement_checks(
                thread_id, history_epoch, entry_id, revision, check_name, flag,
                override_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id, history_epoch, entry_id, revision, check_name, flag)
            DO UPDATE SET override_reason=COALESCE(excluded.override_reason, override_reason)
            """,
            (thread_id, history_epoch, entry_id, revision, CHECK_NAME, flag,
             override_reason, now),
        )


def placement_check_counts(db: sqlite3.Connection, thread_ids: tuple[str, ...]) -> dict[str, Any]:
    """Return honest aggregate fire/override counts for visible lesson placement checks."""
    if not thread_ids:
        fires = overrides = 0
    else:
        # One JSON parameter keeps scope=all inside SQLite's bound-parameter limit.
        row = db.execute(
            """SELECT count(*) AS fires, sum(overridden) AS overrides FROM (
                    SELECT thread_id, history_epoch, entry_id, revision, check_name,
                           max(CASE WHEN override_reason IS NOT NULL THEN 1 ELSE 0 END) AS overridden
                    FROM lesson_placement_checks
                    WHERE thread_id IN (SELECT value FROM json_each(?)) AND check_name=?
                    GROUP BY thread_id, history_epoch, entry_id, revision, check_name
                )""",
            (json.dumps(list(thread_ids)), CHECK_NAME),
        ).fetchone()
        fires, overrides = int(row["fires"] or 0), int(row["overrides"] or 0)
    return {"owner": CHECK_OWNER, "retirement_condition": CHECK_RETIREMENT_CONDITION,
            "fires": fires, "overrides": overrides}


def source_subjects(sources: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """The correction subjects current lesson files name: the one input to the report join."""
    return merge_subjects(*(source.get("originating_subjects", ()) for source in sources))


def correction_facts(db: sqlite3.Connection, subjects: Iterable[str]) -> dict[str, dict[str, str]]:
    """Resolve correction facts by immutable outcome-event subject key.

    A fact whose attributes cannot be read is returned as ``unreadable``, so the
    report says it cannot tell rather than reporting no correction.
    """
    keys = merge_subjects(subjects)
    if not keys:
        return {}
    rows = db.execute(
        """SELECT id, subject_key, ts, attributes
            FROM outcome_events
            WHERE event_name=? AND retracted_at IS NULL
              AND subject_key IN (SELECT value FROM json_each(?))""",
        (CORRECTION_EVENT, json.dumps(list(keys))),
    ).fetchall()
    found: dict[str, dict[str, str]] = {}
    for row in rows:
        subject = str(row["subject_key"])
        try:
            attributes = json.loads(row["attributes"])
        except (TypeError, ValueError):
            logger.warning("correction fact %s has unreadable attributes", row["id"])
            found.setdefault(subject, {"id": str(row["id"]), "ts": str(row["ts"]), "unreadable": "true"})
            continue
        if isinstance(attributes, dict) and attributes.get("label") == "correction":
            found[subject] = {"id": str(row["id"]), "ts": str(row["ts"])}
    return found
