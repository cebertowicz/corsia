"""Admission rules for the reply context a human message carries.

The client sends what the person was answering: the message ranges in view
while they typed, the first message they had not read, and their comments.
Admission checks each item against the thread and stores the
result once as ``metadata.reply_context``; ``core.reply_context`` renders it. A
bad view item is dropped with a recorded reason and the message still sends:
delivery never waits on what was in view. Comments are the person's own words,
so a comment that fails refuses the whole input instead (``RejectedComments``).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from typing import Any, TypeGuard

from infra.chat_comment_ids import format_cid, parse_cid
from infra.failure_reference import new_error_reference

logger = logging.getLogger(__name__)


class RejectedComments(ValueError):
    """Submitted comments failed admission; ``positions`` are 1-based, empty when unknown.

    Its text is shown to the person: which comments, and what to do. The
    composer keeps the draft when a send is refused.
    """

    def __init__(self, positions: list[int], *, reference: str | None = None):
        if reference:
            super().__init__(
                "Couldn’t check your comments, so nothing was sent. Your message and comments "
                f"are kept: try sending again. Reference: {reference}"
            )
            self.positions = positions
            return
        if not positions:
            which, them = "Your comments", "them"
        elif len(positions) == 1:
            which, them = f"Comment {positions[0]}", "that comment"
        else:
            listed = ", ".join(str(position) for position in positions[:-1])
            which, them = f"Comments {listed} and {positions[-1]}", "those comments"
        super().__init__(
            f"{which} couldn’t be matched to text in this chat, so nothing was sent. "
            f"Your message and comments are kept: remove {them} and send again."
        )
        self.positions = positions


def require_every_comment(raw: object, stored: dict | None, dropped: list[dict] | None = None) -> None:
    """Raise ``RejectedComments`` unless every submitted comment was stored.

    A comments drop that carries a ``reference`` was a system failure, logged under it.
    """
    submitted = raw.get("comments") if isinstance(raw, dict) else None
    if submitted is None:
        return
    failed = next((item for item in dropped or [] if item["field"] == "comments" and "reference" in item), None)
    if submitted and failed:
        raise RejectedComments([], reference=failed["reference"])
    if not isinstance(submitted, list):
        raise RejectedComments([])
    kept = {
        parsed[1]
        for item in (stored or {}).get("comments") or []
        if isinstance(item, dict) and (parsed := parse_cid(item.get("cid"))) is not None
    }
    missing = [position for position in range(1, len(submitted) + 1) if position not in kept]
    if missing:
        raise RejectedComments(missing)


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


def comment_cid(input_order: int, index: int) -> str:
    """Return the committed cid for a zero-based raw comment index."""
    return format_cid(input_order, index + 1)


def _comments(db: sqlite3.Connection, thread_id: str, raw: object, newest: int, input_order: int, dropped: list[dict]) -> list[dict]:
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
        root_sender = item.get('root_user_message_id') if isinstance(item, dict) else None
        root_cid = item.get('root_cid') if isinstance(item, dict) else None
        if not reason and (root_sender is not None or root_cid is not None):
            if not isinstance(root_sender, str) or not root_sender or not isinstance(root_cid, str) or not root_cid:
                reason = 'invalid_root'
            else:
                row = db.execute(
                    "SELECT sender.metadata FROM chat_comments c "
                    "JOIN messages sender ON sender.id=c.user_message_id AND sender.thread_id=c.thread_id "
                    "JOIN messages target ON target.id=c.target_message_id AND target.thread_id=c.thread_id "
                    "WHERE c.thread_id=? AND c.user_message_id=? AND c.cid=? "
                    "AND c.root_user_message_id IS NULL AND target.seq=? AND target.role='assistant'",
                    (thread_id, root_sender, root_cid, item['seq']),
                ).fetchone()
                try:
                    original = next((c for c in json.loads(row[0] or '{}').get('reply_context', {}).get('comments', [])
                                     if c.get('cid') == root_cid), None) if row else None
                except (ValueError, TypeError, AttributeError):
                    original = None
                if not original or any(item[key] != original.get(key) for key in (
                    'from_off', 'to_off', 'quote', 'anchor_prefix', 'anchor_suffix',
                )):
                    reason = 'invalid_root'
        if reason:
            dropped.append(_dropped("comments", reason, index=index))
            continue
        # Admission holds the write transaction until input order is incremented.
        cid = comment_cid(input_order, index)
        comment = {key: item[key] for key in (
            "id", "seq", "from_off", "to_off", "quote", "anchor_prefix", "anchor_suffix", "note",
        )} | {"cid": cid}
        if root_sender is not None:
            comment.update(root_user_message_id=root_sender, root_cid=root_cid)
        comments.append(comment)
    return comments


def validate_reply_context(
    db: sqlite3.Connection, thread_id: str, raw: object, *, dropped: list[dict] | None = None,
) -> dict | None:
    """Return the reply context to store for one new human message, or None.

    A field that fails is dropped on its own; counts by reason are logged.
    Nothing here raises into admission.
    """
    if raw is None:
        return None
    if dropped is None:
        dropped = []
    stored: dict[str, Any] = {}
    if not isinstance(raw, dict):
        dropped.append(_dropped("reply_context", "not_object"))
    else:
        newest = 0
        unreadable = None
        try:
            newest = int(db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM messages WHERE thread_id=?",
                (thread_id,),
            ).fetchone()[0])
        except sqlite3.Error:
            unreadable = new_error_reference()
            logger.warning("reply context: thread seq range unreadable reference=%s", unreadable, exc_info=True)
        builders = (
            ("in_view", lambda value: _in_view(value, newest, dropped)),
            ("first_unread", lambda value: _first_unread(value, newest, dropped)),
            ("comments", lambda value: _comments(db, thread_id, value, newest, int(db.execute(
                "SELECT next_input_order FROM threads WHERE id=?", (thread_id,),
            ).fetchone()[0]), dropped)),
        )
        for field, build in builders:
            if raw.get(field) is None:
                continue
            if unreadable:
                dropped.append(_dropped(field, "thread_unreadable", reference=unreadable))
                continue
            try:
                result = build(raw[field])
            except Exception:
                reference = new_error_reference()
                logger.warning("reply context: %s dropped reference=%s", field, reference, exc_info=True)
                dropped.append(_dropped(field, "validation_error", reference=reference))
                continue
            if result:
                stored[field] = result
    if dropped:
        counts = Counter(f'{item["field"]}:{item["reason"]}' for item in dropped)
        logger.info("reply context: thread=%s dropped=%s", thread_id, dict(sorted(counts.items())))
    return stored or None
