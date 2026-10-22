"""Admission rules for the reply context a human message carries.

The client sends what the person was answering: the message ranges in view
while they typed and the first message they had not read (comments arrive with
the comment UI). Admission checks each item against the thread and stores the
result once as ``metadata.reply_context``; ``core.reply_context`` renders it. A
bad item is dropped with a recorded reason and the message still sends:
delivery never waits on reply context.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, TypeGuard

logger = logging.getLogger(__name__)

IN_VIEW_RANGE_CAP = 8
# Raw client ranges parsed before merging; the newest are kept.
IN_VIEW_INPUT_CAP = 64
DROPPED_CAP = 20


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _dropped(field: str, reason: str, **detail: Any) -> dict:
    return {"field": field, "reason": reason, **detail}


def _range(raw: object, newest: int) -> tuple[int, int, int | None] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) not in (2, 3):
        return None
    start, end = raw[0], raw[1]
    if not _is_int(start) or not _is_int(end) or not 1 <= start <= end <= newest:
        return None
    shown = raw[2] if len(raw) == 3 else None
    if _is_int(shown):
        return start, end, min(max(shown, 1), end - start + 1)
    return start, end, None


def _merge(ranges: list[tuple[int, int, int | None]]) -> list[tuple[int, int, int | None]]:
    """Sort and join ranges that overlap or touch, summing the rows shown."""
    merged: list[tuple[int, int, int | None]] = []
    for start, end, shown in sorted(ranges, key=lambda item: (item[0], item[1])):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end, shown))
            continue
        first, last_end, last_shown = merged[-1]
        joined_end = max(last_end, end)
        joined_shown = (
            None if last_shown is None or shown is None
            else min(last_shown + shown, joined_end - first + 1)
        )
        merged[-1] = (first, joined_end, joined_shown)
    return merged


def _in_view(raw: object, newest: int, dropped: list[dict]) -> list[list[int | None]]:
    if not isinstance(raw, list):
        dropped.append(_dropped("in_view", "not_list"))
        return []
    if len(raw) > IN_VIEW_INPUT_CAP:
        dropped.append(_dropped(
            "in_view", "over_cap", count=len(raw) - IN_VIEW_INPUT_CAP,
        ))
        raw = raw[-IN_VIEW_INPUT_CAP:]
    ranges = []
    for item in raw:
        parsed = _range(item, newest)
        if parsed is None:
            dropped.append(_dropped("in_view", "invalid_range"))
        else:
            ranges.append(parsed)
    merged = _merge(ranges)
    if len(merged) > IN_VIEW_RANGE_CAP:
        dropped.append(_dropped(
            "in_view", "over_cap", count=len(merged) - IN_VIEW_RANGE_CAP,
        ))
        merged = merged[-IN_VIEW_RANGE_CAP:]
    return [list(item) for item in merged]


def _first_unread(raw: object, newest: int, dropped: list[dict]) -> dict | None:
    if not isinstance(raw, dict):
        dropped.append(_dropped("first_unread", "invalid_seq"))
        return None
    seq = raw.get("seq")
    if not _is_int(seq) or not 1 <= seq <= newest:
        dropped.append(_dropped("first_unread", "invalid_seq"))
        return None
    return {"seq": seq, "truncated": raw.get("truncated") is True}


def validate_reply_context(
    db: sqlite3.Connection, thread_id: str, raw: object,
) -> dict | None:
    """Return the reply context to store for one new human message, or None.

    A field that fails is dropped on its own, with its reason under
    ``dropped``; nothing here raises into admission.
    """
    if raw is None:
        return None
    dropped: list[dict] = []
    stored: dict[str, Any] = {}
    if not isinstance(raw, dict):
        dropped.append(_dropped("reply_context", "not_object"))
    else:
        newest = 0
        unreadable = False
        try:
            newest = int(db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM messages WHERE thread_id=?",
                (thread_id,),
            ).fetchone()[0])
        except sqlite3.Error:
            unreadable = True
            logger.warning("reply context: thread seq range unreadable", exc_info=True)
        builders = (
            ("in_view", lambda value: _in_view(value, newest, dropped)),
            ("first_unread", lambda value: _first_unread(value, newest, dropped)),
        )
        for field, build in builders:
            if raw.get(field) is None:
                continue
            if unreadable:
                dropped.append(_dropped(field, "thread_unreadable"))
                continue
            try:
                result = build(raw[field])
            except Exception:
                logger.warning("reply context: %s dropped", field, exc_info=True)
                dropped.append(_dropped(field, "validation_error"))
                continue
            if result:
                stored[field] = result
    if dropped:
        stored["dropped"] = dropped[:DROPPED_CAP]
    return stored or None
