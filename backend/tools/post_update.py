"""Durable, explicitly user-directed agent updates."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import partial
from typing import Any

from infra import thread_updates
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.queries import insert_message
from infra.send_broadcast import broadcast_message_rows
from infra.time import utc_now_iso
from tools.artifact_tool_helpers import causal_turn_id
from tools.base import Tool
from tools.post_update_text import replacement_args, refuse
from tools.catalog_entries import POST_UPDATE_ATTENTION_GUIDANCE, POST_UPDATE_SUMMARY

log = logging.getLogger(__name__)

_MAX_TEXT_LENGTH = 2_000
_IDEMPOTENCY_METADATA_KEY = "post_update_idempotency_key"


def _clean_text(value: Any) -> str | None:
    """Return trimmed non-empty text only."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _own_plan_node(db, thread_id: str, node_id: str) -> bool:
    """Whether an unarchived node belongs to the calling thread's plan."""
    return db.execute(
        """SELECT 1
           FROM plan_nodes AS node
           JOIN plans AS plan ON plan.id = node.plan_id
           WHERE node.id=? AND node.archived_at IS NULL
             AND plan.owner_kind='thread' AND plan.owner_id=?""",
        (node_id, thread_id),
    ).fetchone() is not None


_KEYED_SQL = """SELECT id, content, metadata FROM messages
   WHERE thread_id=? AND json_extract(metadata, '$.post_update_idempotency_key')=?
   {tail} ORDER BY seq DESC LIMIT 1"""
_SIBLING_SQL = """SELECT id, content, metadata FROM messages
   WHERE thread_id=? AND role='assistant' AND json_valid(metadata)
     AND json_extract(metadata, '$.turn_id')=?
     AND COALESCE(json_extract(metadata, '$.for_user'), 0)=0
   {tail} ORDER BY seq DESC LIMIT 1"""


def _newest(db, sql: str, thread_id: str, probe: str, first_unread: int | None):
    """Newest matching message, only from transcript position ``first_unread`` on when given."""
    if first_unread is None:
        return db.execute(sql.format(tail=""), (thread_id, probe)).fetchone()
    return db.execute(
        sql.format(tail="AND seq>=?"), (thread_id, probe, first_unread),
    ).fetchone()


@dataclass(frozen=True)
class _ReadAhead:
    """Transcript scans done before the write lock was taken.

    ``first_unread`` is the thread's next transcript position when the scans
    started: every row below it was already committed and covered by them, so
    the locked pass reads only the rows from there on. ``None`` means no scan
    ran and the locked pass does the full lookup.
    """

    first_unread: int | None = None
    prior_id: str | None = None
    sibling_id: str | None = None
    sibling_scanned: bool = False


def _read_ahead(
    thread_id: str, idempotency_key: str | None, turn_id: str,
) -> _ReadAhead:
    """Run the unbounded transcript lookups without holding the write lock."""
    db = get_db()
    row = db.execute(
        "SELECT next_message_seq FROM threads WHERE id=?", (thread_id,),
    ).fetchone()
    if row is None:
        return _ReadAhead()
    prior = (
        _newest(db, _KEYED_SQL, thread_id, idempotency_key, None)
        if idempotency_key is not None else None
    )
    sibling_scanned = _turn_is_running(db, thread_id, turn_id)
    sibling = _newest(db, _SIBLING_SQL, thread_id, turn_id, None) if sibling_scanned else None
    return _ReadAhead(
        first_unread=int(row[0]),
        prior_id=str(prior["id"]) if prior is not None else None,
        sibling_id=str(sibling["id"]) if sibling is not None else None,
        sibling_scanned=sibling_scanned,
    )


def _resolve_newest(
    db, sql: str, thread_id: str, probe: str, first_unread: int | None, ahead_id: str | None,
):
    """The newest match as a full scan would find it, using the read-ahead's result."""
    if first_unread is None:
        return _newest(db, sql, thread_id, probe, None)
    newest = _newest(db, sql, thread_id, probe, first_unread)
    if newest is not None or ahead_id is None:
        return newest
    found = db.execute(
        "SELECT id, content, metadata FROM messages WHERE id=? AND thread_id=?",
        (ahead_id, thread_id),
    ).fetchone()
    # A row deleted since the read-ahead leaves older matches unknown: rescan.
    return found if found is not None else _newest(db, sql, thread_id, probe, None)


def _turn_is_running(db, thread_id: str, turn_id: str) -> bool:
    return db.execute(
        """SELECT 1 FROM active_turns
           WHERE id=? AND thread_id=? AND status='running'""",
        (turn_id, thread_id),
    ).fetchone() is not None


def _running_turn_model(db, thread_id: str, turn_id: str) -> str | None:
    row = db.execute(
        "SELECT model FROM active_turns WHERE id=? AND thread_id=?",
        (turn_id, thread_id),
    ).fetchone()
    return _clean_text(row["model"]) if row is not None else None


def _running_turn_model_metadata(
    db, thread_id: str, turn_id: str, ahead: _ReadAhead,
) -> dict[str, Any]:
    """Return sibling identity, falling back to the running turn's exact model."""
    if not _turn_is_running(db, thread_id, turn_id):
        return {}
    turn_model = _running_turn_model(db, thread_id, turn_id)
    row = _resolve_newest(
        db, _SIBLING_SQL, thread_id, turn_id,
        ahead.first_unread if ahead.sibling_scanned else None, ahead.sibling_id,
    )
    fallback = {"model": turn_model} if turn_model is not None else {}
    if row is None:
        return fallback
    from api.thread_message_rows import metadata_dict

    sibling_metadata = metadata_dict(row)
    if _clean_text(sibling_metadata.get("model")) is None:
        # A sibling without a usable model has no coherent identity: its other fields
        # would mislabel the turn's own model, so the turn is the only source.
        return fallback
    return {
        key: sibling_metadata[key]
        for key in ("model", "model_name", "is_cloud", "cc", "codex", "harness")
        if key in sibling_metadata
    }


def _persist_update(thread_id, text, node_id, idempotency_key, turn_id, replace=(), replace_reason=None):
    """Write a complete replacement and its exact receipt under one write transaction."""
    from contextlib import ExitStack
    from infra.message_update_seen import snapshot
    from infra.message_update_state import apply_replacement, register, unseen_updates, validate_targets
    from tools.post_update_text import result_text
    from tools.post_update_retry import replay_result

    ahead = _read_ahead(thread_id, idempotency_key, turn_id)
    with ExitStack() as stack:
        with write_transaction(None, scope="post_update") as db:
            owner = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
            if owner is None:
                raise ValueError("The calling chat no longer exists.")
            user_id = str(owner["user_id"])
            if idempotency_key is not None:
                prior = _resolve_newest(db, _KEYED_SQL, thread_id, idempotency_key,
                                        ahead.first_unread, ahead.prior_id)
                if prior is not None:
                    saved = replay_result(db, prior, text, node_id, replace, replace_reason)
                    return str(prior["id"]), True, user_id, saved
            if node_id is not None and not _own_plan_node(db, thread_id, node_id):
                raise ValueError("node_id is not a node in your plan; omit it or use a node from plan_read.")
            live = stack.enter_context(snapshot(user_id, thread_id))
            now = utc_now_iso()
            validate_targets(db, thread_id, user_id, replace, live, now)
            metadata = {"for_user": True, "turn_id": turn_id,
                        **_running_turn_model_metadata(db, thread_id, turn_id, ahead)}
            if node_id is not None:
                metadata["node_id"] = node_id
            if idempotency_key is not None:
                metadata[_IDEMPOTENCY_METADATA_KEY] = idempotency_key
            if replace:
                metadata["post_update_replace"] = list(replace)
                metadata["post_update_replace_reason"] = replace_reason
            message_id = insert_message(thread_id, "assistant", text, metadata, db=db, created_at=now)
            register(db, message_id, thread_id, "post_update", node_id)
            apply_replacement(db, thread_id, message_id, replace, replace_reason, now)
            thread_updates.record_marked(db, thread_id=thread_id, message_id=message_id,
                                         node_id=node_id, created_at=now)
            rows = unseen_updates(db, thread_id, user_id, message_id, live, now)
            saved = result_text(message_id, replace, rows, thread_id)
            db.execute("UPDATE message_update_state SET result_text=?,result_chars=? WHERE message_id=?",
                       (saved, len(saved), message_id))
            return message_id, False, user_id, saved


class PostUpdateTool(Tool):
    """Let an agent leave a durable update for the user to read later."""

    self_describing_errors = True

    @property
    def name(self) -> str:
        return "post_update"

    @property
    def description(self) -> str:
        return POST_UPDATE_SUMMARY + ". " + POST_UPDATE_ATTENTION_GUIDANCE

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The concise update for the user.",
                },
                "node_id": {
                    "type": "string",
                    "description": "Optional node from your plan_read result to associate with this update.",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional stable retry key for this one update.",
                },
                "replace": {
                    "type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True,
                    "description": "Your own unseen update ids in this chat to replace together; omit for a new subject.",
                },
                "replace_reason": {
                    "type": "string", "enum": ["corrects", "updates"],
                    "description": "Required with replace: corrects a wrong claim, or updates the same subject.",
                },
            },
            "required": ["text"],
        }

    async def execute(self, **kwargs: Any) -> str:
        """Persist and broadcast one validated update for the current thread."""
        thread_id = kwargs.get("_thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            return refuse("The calling chat could not be resolved.")
        text = _clean_text(kwargs.get("text"))
        if text is None:
            return refuse("text must be non-empty after trimming.")
        if len(text) > _MAX_TEXT_LENGTH:
            return refuse("text must be at most 2,000 characters; put longer detail in a linked document.")
        turn_id = causal_turn_id(kwargs)
        if not turn_id:
            return refuse("The calling turn could not be resolved.")
        node_id = _clean_text(kwargs.get("node_id")) if "node_id" in kwargs else None
        if "node_id" in kwargs and node_id is None:
            return refuse("node_id must be non-empty when passed; omit it or use a node from plan_read.")
        idempotency_key = (
            _clean_text(kwargs.get("idempotency_key"))
            if "idempotency_key" in kwargs
            else None
        )
        if "idempotency_key" in kwargs and idempotency_key is None:
            return refuse("idempotency_key must be non-empty when passed.")
        try:
            replace, replace_reason = replacement_args(kwargs)
            message_id, duplicate, user_id, saved = await run_in_db(
                partial(_persist_update, thread_id, text, node_id, idempotency_key, turn_id, replace, replace_reason),
            )
        except ValueError as exc:
            return refuse(str(exc))
        except Exception as exc:
            return refuse(f"The update transaction failed ({type(exc).__name__}); retry with the same idempotency_key.")
        if not duplicate:
            log.info("post_update.sent thread=%s turn=%s", thread_id, turn_id)
            thread_updates.notify_changed(user_id, [thread_id])
            try:
                if replace:
                    from infra.send_broadcast import broadcast_replacement

                    await broadcast_replacement(thread_id, message_id, replace)
                else:
                    await broadcast_message_rows(thread_id, (message_id,))
            except Exception:
                # The committed message remains visible after reload.
                log.warning("post_update broadcast failed for message %s", message_id, exc_info=True)
        return saved
