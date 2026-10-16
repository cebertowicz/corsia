"""Compact terminal context receipts stored on exact transcript messages."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from infra.database import get_db, write_transaction


_EXTERNAL_HARNESSES = frozenset({"claude-code", "codex"})
_HANDLE_RE = re.compile(r"^[0-9a-f]{64}$")
_SUMMARY_FIELDS = (
    "model_context_window",
    "discovered_context_window",
    "system_tokens",
    "tool_def_tokens",
    "history_tokens",
    "user_tokens",
    "context_source_tokens",
    "total_used",
    "utilization",
    "provider_reported_input_tokens",
    "provider_reported_cached_input_tokens",
    "provider_reported_cache_read_input_tokens",
    "provider_reported_cache_creation_input_tokens",
    "provider_reported_total_used",
    "provider_reported_utilization",
    "attribution_delta_tokens",
    "attribution_delta_reason",
    "token_usage_available",
    "token_source",
    "stale_reason",
    "history_source_attribution",
    "was_trimmed",
    "history_messages",
    "snapshot_id",
    "content_hash",
    "continuity_mode",
    "continuity_block_bytes",
    "continuity_block_tokens",
    "continuity_block_reason",
    "preview",
)
_SCALAR_TYPES = (str, int, float, bool, type(None))


@dataclass(frozen=True, slots=True)
class ContextReceiptIdentity:
    """Exact provider context that produced one terminal measurement."""

    harness: str
    catalog_model_id: str
    runtime_context_epoch: int
    producer_handle_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.harness not in {"native", *_EXTERNAL_HARNESSES}:
            raise ValueError("unsupported context receipt harness")
        if not isinstance(self.catalog_model_id, str) or not self.catalog_model_id:
            raise ValueError("catalog_model_id must be a non-empty string")
        if (
            isinstance(self.runtime_context_epoch, bool)
            or not isinstance(self.runtime_context_epoch, int)
            or self.runtime_context_epoch < 0
        ):
            raise ValueError("runtime_context_epoch must be a non-negative integer")
        fingerprint = self.producer_handle_fingerprint
        if self.harness in _EXTERNAL_HARNESSES:
            if not isinstance(fingerprint, str) or not _HANDLE_RE.fullmatch(fingerprint):
                raise ValueError("external receipts require an opaque handle fingerprint")
        elif fingerprint is not None:
            raise ValueError("native receipts cannot carry an external handle fingerprint")


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def producer_handle_fingerprint(frozen_identity: Any) -> str:
    """Hash one frozen handle identity without exposing its fields."""
    if is_dataclass(frozen_identity) and not isinstance(frozen_identity, type):
        payload = asdict(frozen_identity)
    elif isinstance(frozen_identity, dict):
        payload = frozen_identity
    else:
        raise TypeError("producer handle identity must be a frozen dataclass or dict")
    canonical = json.dumps(
        _jsonable(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def current_context_receipt_identity(
    thread_id: str,
    *,
    harness: str,
    catalog_model_id: str,
    runtime_context_epoch: int,
) -> ContextReceiptIdentity | None:
    """Return the exact current trusted producer identity for one target."""
    if harness == "native":
        return ContextReceiptIdentity(harness, catalog_model_id, runtime_context_epoch)
    if harness == "claude-code":
        from subagents import cc_runtime

        handle = cc_runtime.trusted_handle_fingerprint(thread_id)
    elif harness == "codex":
        from subagents.codex_runtime_state import trusted_handle_fingerprint

        handle = trusted_handle_fingerprint(thread_id)
    else:
        return None
    if (
        handle is None
        or getattr(handle, "catalog_model_id", None) != catalog_model_id
        or getattr(handle, "runtime_context_epoch", None) != runtime_context_epoch
    ):
        return None
    return ContextReceiptIdentity(
        harness,
        catalog_model_id,
        runtime_context_epoch,
        producer_handle_fingerprint(handle),
    )


def _loaded_tool_entries(value: Any) -> list[dict[str, Any]]:
    """Keep only well-formed per-app lookup rows, so a bad list never widens the receipt."""
    if not isinstance(value, list):
        return []
    entries = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        server, tokens = entry.get("server"), entry.get("tokens")
        display = entry.get("display_name")
        if (
            not isinstance(server, str) or not server
            or isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0
            or not (display is None or isinstance(display, str))
        ):
            continue
        entries.append({"server": server, "display_name": display or server, "tokens": tokens})
    return entries


def context_usage_summary(context_usage: dict[str, Any]) -> dict[str, Any]:
    """Return the compact CTX fields safe for message and websocket payloads.

    Every field is scalar except ``loaded_tool_tokens``, the per-app lookup rows
    the context sheet shows after a turn.
    """
    summary = {
        field: context_usage[field]
        for field in _SUMMARY_FIELDS
        if field in context_usage and isinstance(context_usage[field], _SCALAR_TYPES)
    }
    if "loaded_tool_tokens" in context_usage:
        summary["loaded_tool_tokens"] = _loaded_tool_entries(context_usage["loaded_tool_tokens"])
    return summary


def live_context_usage_payload(context_usage: dict[str, Any]) -> dict[str, Any]:
    """Project one live CTX reading without widening the stored receipt."""
    payload = context_usage_summary(context_usage)
    snapshot = context_usage.get("context_snapshot")
    if not isinstance(snapshot, dict):
        return payload
    projection = {
        field: snapshot[field]
        for field in ("snapshot_id", "source_inventory")
        if field in snapshot
    }
    if projection:
        payload["context_snapshot"] = projection
    return payload


def positive_window(value: int | None, *, absent: int | None) -> int | None:
    """Keep a positive window, or report its absence as ``absent``."""
    usable = (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    )
    return value if usable else absent


def unavailable_context_usage(
    *,
    model_context_window: int | None,
    stale_reason: str,
    discovered_context_window: int | None = None,
) -> dict[str, Any]:
    """Represent unavailable occupancy without inventing a token total."""
    window = positive_window(model_context_window, absent=0)
    return {
        "model_context_window": window,
        "discovered_context_window": positive_window(
            discovered_context_window, absent=None,
        ),
        "system_tokens": 0,
        "tool_def_tokens": 0,
        "history_tokens": 0,
        "user_tokens": 0,
        "total_used": None,
        "utilization": None,
        "token_usage_available": False,
        "stale_reason": stale_reason,
        "was_trimmed": False,
        "history_messages": 0,
    }


def context_usage_for_boundary(
    *,
    snapshot: dict[str, Any] | Any,
    source_artifacts: list[dict[str, Any]] | None = None,
    continuity_mode: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap one live provider-bound snapshot in the CTX usage shape."""
    if hasattr(snapshot, "to_dict"):
        snapshot = snapshot.to_dict()
    if not isinstance(snapshot, dict):
        raise ValueError("context boundary snapshot must be a dictionary")
    usage = {
        "snapshot_id": snapshot.get("snapshot_id"),
        "context_snapshot": snapshot,
        "continuity_mode": continuity_mode or "full",
        "source_artifacts": source_artifacts or [],
    }
    if snapshot.get("content_hash"):
        usage["content_hash"] = snapshot["content_hash"]
    if extra:
        usage.update(extra)
    from infra.lesson_usage_attribution import attach_lesson_estimates

    attach_lesson_estimates(usage)
    return _jsonable(usage)


def context_usage_receipt(
    context_usage: dict[str, Any],
    identity: ContextReceiptIdentity,
) -> dict[str, Any]:
    """Bind one compact usage projection to its exact provider context."""
    receipt = context_usage_summary(context_usage)
    receipt.update(
        harness=identity.harness,
        catalog_model_id=identity.catalog_model_id,
        runtime_context_epoch=identity.runtime_context_epoch,
    )
    if identity.producer_handle_fingerprint is not None:
        receipt["producer_handle_fingerprint"] = identity.producer_handle_fingerprint
    return receipt


def attach_context_receipt(
    thread_id: str,
    message_id: str,
    *,
    context_usage: dict[str, Any],
    identity: ContextReceiptIdentity,
    db=None,
) -> dict[str, Any] | None:
    """Attach one compact receipt to an exact assistant or result message."""
    if not thread_id or not message_id or not isinstance(context_usage, dict):
        return None
    with write_transaction(db, scope="attach_context_receipt") as connection:
        row = connection.execute(
            "SELECT role,metadata FROM messages WHERE thread_id=? AND id=?",
            (thread_id, message_id),
        ).fetchone()
        if row is None:
            return None
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        turn_id = metadata.get("turn_id") if isinstance(metadata, dict) else None
        if not isinstance(turn_id, str) or not turn_id.strip():
            return None
        if row["role"] != "assistant" and not (
            row["role"] == "system" and metadata.get("type") == "result"
        ):
            return None
        receipt = context_usage_receipt(context_usage, identity)
        metadata["context_usage"] = receipt
        connection.execute(
            "UPDATE messages SET metadata=? WHERE thread_id=? AND id=?",
            (json.dumps(metadata, sort_keys=True), thread_id, message_id),
        )
        return receipt


def attach_delivered_context_to_latest_assistant(
    *,
    thread_id: str,
    step_ids: list[str],
    context_usage: dict,
    identity: ContextReceiptIdentity,
) -> tuple[str, dict] | None:
    """Attach a terminal receipt to the latest delivered assistant step."""
    if not context_usage or not step_ids:
        return None
    with write_transaction(None, scope="attach_delivered_context") as db:
        placeholders = ",".join("?" for _ in step_ids)
        row = db.execute(
            f"""
            SELECT id FROM messages
            WHERE thread_id = ? AND role = 'assistant' AND id IN ({placeholders})
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (thread_id, *step_ids),
        ).fetchone()
        if not row:
            return None
        receipt = attach_context_receipt(
            thread_id,
            row["id"],
            context_usage=context_usage,
            identity=identity,
            db=db,
        )
        if receipt is None:
            return None
        return str(row["id"]), receipt


def _matches_measured_identity(
    receipt: object,
    identity: ContextReceiptIdentity,
) -> bool:
    if not isinstance(receipt, dict):
        return False
    total = receipt.get("total_used")
    window = receipt.get("model_context_window")
    if (
        receipt.get("token_usage_available") is not True
        or receipt.get("token_source") != "measured"
        or isinstance(total, bool)
        or not isinstance(total, (int, float))
        or total < 0
        or isinstance(window, bool)
        or not isinstance(window, (int, float))
        or window <= 0
    ):
        return False
    return (
        receipt.get("harness") == identity.harness
        and receipt.get("catalog_model_id") == identity.catalog_model_id
        and receipt.get("runtime_context_epoch") == identity.runtime_context_epoch
        and receipt.get("producer_handle_fingerprint")
        == identity.producer_handle_fingerprint
    )


def latest_measured_context_receipt(
    thread_id: str,
    identity: ContextReceiptIdentity,
    *,
    db=None,
) -> dict[str, Any] | None:
    """Return a measured receipt only from the latest terminal Turn."""
    owns_db = db is None
    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT id,metadata FROM messages WHERE thread_id=? "
            "AND json_valid(metadata) "
            "AND json_extract(metadata,'$.turn_id')=("
            "SELECT id FROM active_turns WHERE thread_id=? "
            "AND status IN ('completed','cancelled','error') "
            "ORDER BY turn_order DESC LIMIT 1) "
            "AND json_type(metadata,'$.context_usage')='object' "
            "ORDER BY seq DESC,rowid DESC LIMIT 1",
            (thread_id, thread_id),
        ).fetchone()
        if row is None:
            return None
        metadata = json.loads(row["metadata"])
        receipt = metadata.get("context_usage")
        if not _matches_measured_identity(receipt, identity):
            return None
        return {"message_id": str(row["id"]), "context_usage": receipt}
    finally:
        if owns_db:
            connection.close()
