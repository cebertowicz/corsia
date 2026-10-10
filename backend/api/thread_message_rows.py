"""Canonical parsing and public projection for session message rows."""

from __future__ import annotations

import json


def metadata_dict(row) -> dict:
    """Parse one message row's metadata as an object or an empty object."""
    raw = row["metadata"] if "metadata" in row.keys() else None
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def public_delivery_message(row) -> dict:
    """Render one durable inbox row without exposing private attempt data."""
    metadata = {
        key: value
        for key, value in metadata_dict(row).items()
        if not key.startswith("_corsia_")
    }
    metadata.pop("delivery_intent", None)
    if row["delivery_intent"] == "queue_after_turn":
        metadata["delivery_intent"] = "queue_after_turn"
    return {
        "id": str(row["id"]),
        "role": str(row["role"]),
        "content": str(row["content"]),
        "metadata": metadata,
        "created_at": str(row["created_at"]),
        "delivery_state": str(row["delivery_state"]),
        "delivery_error": row["delivery_error"],
        "delivery_revision": int(row["delivery_revision"]),
        "seq": int(row["seq"]) if row["seq"] is not None else None,
        "input_order": (
            int(row["input_order"]) if row["input_order"] is not None else None
        ),
        "active_turn_id": row["active_turn_id"],
        "write_started_at": row["write_started_at"],
        "write_outcome": row["write_outcome"],
    }
