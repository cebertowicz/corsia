"""Admission rules for the reply context a human message carries.

The client sends what the person was answering: the message ranges in view
while they typed, the first message they had not read, and their comments.
Admission checks each item against the thread and stores the
result once as ``metadata.reply_context``; ``core.reply_context`` renders it. A
bad item is dropped with a recorded reason and the message still sends:
delivery never waits on reply context.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from typing import Any, TypeGuard

logger = logging.getLogger(__name__)


class EmptyCommentInput(ValueError):
    """An empty input had no valid comments after admission validation."""


def has_comment_candidates(raw: object) -> bool:
    """Whether an input presents any comments for admission to validate."""
    return isinstance(raw, dict) and isinstance(raw.get("comments"), list) and bool(raw["comments"])


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
    """Join overlapping or touching ranges, keeping the larger shown count."""
    merged: list[tuple[int, int, int | None]] = []
    for start, end, shown in sorted(ranges, key=lambda item: (item[0], item[1])):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end, shown))
            continue
        first, last_end, last_shown = merged[-1]
        joined_end = max(last_end, end)
        joined_shown = (
            None if last_shown is None or shown is None
            else min(max(last_shown, shown), joined_end - first + 1)
        )
        merged[-1] = (first, joined_end, joined_shown)
    return merged


def _in_view(raw: object, newest: int, dropped: list[dict]) -> list[list[int | None]]:
    if not isinstance(raw, list):
        dropped.append(_dropped("in_view", "not_list"))
        return []
    ranges = []
    for item in raw:
        parsed = _range(item, newest)
        if parsed is None:
            dropped.append(_dropped("in_view", "invalid_range"))
        else:
            ranges.append(parsed)
    merged = _merge(ranges)
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


def _comments(raw: object, newest: int, input_order: int, dropped: list[dict]) -> list[dict]:
    if not isinstance(raw, list):
        dropped.append(_dropped("comments", "not_list"))
        return []
    comments = []
    for index, item in enumerate(raw):
        reason = None
        if not isinstance(item, dict):
            reason = "not_object"
        elif not _is_int(item.get("seq")) or not 1 <= item["seq"] <= newest:
            reason = "invalid_seq"
        elif (not _is_int(item.get("from_off")) or not _is_int(item.get("to_off"))
              or not 0 <= item["from_off"] < item["to_off"]):
            reason = "invalid_offsets"
        elif not isinstance(item.get("id"), str) or not item["id"]:
            reason = "invalid_id"
        elif any(not isinstance(item.get(key), str) for key in (
            "quote", "anchor_prefix", "anchor_suffix", "note",
        )):
            reason = "invalid_string"
        elif not item["quote"]:
            reason = "empty_quote"
        if reason:
            dropped.append(_dropped("comments", reason, index=index))
            continue
        # Admission holds the write transaction until input order is incremented.
        cid = f"c{input_order}.{index + 1}"
        comments.append({key: item[key] for key in (
            "id", "seq", "from_off", "to_off", "quote", "anchor_prefix", "anchor_suffix", "note",
        )} | {"cid": cid})
    return comments


def validate_reply_context(
    db: sqlite3.Connection, thread_id: str, raw: object,
) -> dict | None:
    """Return the reply context to store for one new human message, or None.

    A field that fails is dropped on its own; counts by reason are logged.
    Nothing here raises into admission.
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
            ("comments", lambda value: _comments(value, newest, int(db.execute(
                "SELECT next_input_order FROM threads WHERE id=?", (thread_id,),
            ).fetchone()[0]), dropped)),
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
        counts = Counter(f'{item["field"]}:{item["reason"]}' for item in dropped)
        logger.info("reply context: thread=%s dropped=%s", thread_id, dict(sorted(counts.items())))
    return stored or None
