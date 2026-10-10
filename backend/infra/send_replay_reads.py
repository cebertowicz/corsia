"""Durable reads behind send preflight: provider capacity and replay candidates."""

from __future__ import annotations

import json
from dataclasses import dataclass

import infra.database as _database
from llm.resolution import ModelUnavailableError

@dataclass(frozen=True, slots=True)
class ProviderCapacity:
    """The latest measured provider occupancy plus the tail written after it."""

    measured_tokens: int
    tail_tokens: int
    harness: str
    catalog_model_id: str


def _metadata(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _terminal_tail_tokens(db, thread_id: str, message_id: str, *, model: str | None = None) -> int | None:
    from core.token_estimation import _text_tokens

    row = db.execute(
        "SELECT id,role,content,metadata FROM messages "
        "WHERE thread_id=? AND id=?",
        (thread_id, message_id),
    ).fetchone()
    if row is None:
        return None
    metadata = _metadata(row["metadata"])
    tokens = _text_tokens(str(row["content"] or ""), model) + 4
    for field in ("reasoning", "tool_calls"):
        value = metadata.get(field)
        if value:
            tokens += _text_tokens(
                value if isinstance(value, str) else json.dumps(value, sort_keys=True), model,
            )
    if row["role"] != "assistant":
        return tokens
    results = db.execute(
        "SELECT content FROM messages WHERE thread_id=? AND role='tool' "
        "AND json_valid(metadata) "
        "AND json_extract(metadata,'$.assistant_step_id')=? ORDER BY seq",
        (thread_id, str(row["id"])),
    ).fetchall()
    return tokens + sum(
        _text_tokens(str(result["content"] or ""), model) + 4 for result in results
    )


def read_provider_capacity(
    thread_id: str, *, db=None,
) -> ProviderCapacity | None:
    """Read the newest measured provider occupancy for the thread's model."""
    from infra.context_receipts import (
        current_context_receipt_identity,
        latest_measured_context_receipt,
    )
    from llm.resolution import resolve_execution_target

    connection = db or _database.get_db()
    try:
        thread_row = connection.execute(
            "SELECT model,runtime_context_epoch FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone()
        if thread_row is None:
            return None
        target = resolve_execution_target(str(thread_row["model"] or ""), db=connection)
        identity = current_context_receipt_identity(
            thread_id,
            harness=target.harness,
            catalog_model_id=target.catalog_id,
            runtime_context_epoch=int(thread_row["runtime_context_epoch"]),
        )
        if identity is None:
            return None
        record = latest_measured_context_receipt(
            thread_id,
            identity,
            db=connection,
        )
        usage = record.get("context_usage") if record is not None else None
        total = usage.get("total_used") if isinstance(usage, dict) else None
        if usage is None or usage.get("token_usage_available") is not True:
            return None
        if isinstance(total, bool) or not isinstance(total, (int, float)) or total < 0:
            return None
        tail = _terminal_tail_tokens(
            connection, thread_id, str(record["message_id"]),
            model=target.expected_reported_model or target.transport_model,
        )
        if tail is None:
            return None
        return ProviderCapacity(int(total), tail, target.harness, target.catalog_id)
    except (ModelUnavailableError, ValueError, RuntimeError):
        return None
    finally:
        if db is None:
            connection.close()


def read_candidates(thread_id: str, exclude_message_id: str | None = None):
    """Freeze the same canonical replay used by warming and checkpoint capture."""
    from core.context_replay import read_replay_candidates

    connection = _database.get_db()
    try:
        connection.execute("BEGIN")
        return read_replay_candidates(
            thread_id, db=connection, exclude_message_id=exclude_message_id,
        )
    finally:
        connection.close()
