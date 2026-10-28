"""Append a provider tool declaration to an existing Assistant Step."""

import json

from infra.queries import assert_turn_accepts_tool_calls


def append_tool_use_locked(
    child_thread_id: str,
    assistant_step_id: str,
    *,
    turn_id: str | None,
    tool_id: str,
    name: str,
    arguments: dict,
    hint: str | None,
    provider_turn_id: str,
    db,
) -> str:
    """Append under the caller's transaction, preserving turn admission and CAS."""
    # The other declaration shape: this appends a call to an existing Step by
    # UPDATE, so it never reaches insert_message's admission check, and it
    # creates exactly the same debt an INSERT would.
    if turn_id is not None:
        assert_turn_accepts_tool_calls(db, turn_id)
    row = db.execute(
        "SELECT metadata FROM messages WHERE id=? AND thread_id=? "
        "AND role='assistant'",
        (assistant_step_id, child_thread_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"missing Assistant Step {assistant_step_id!r}")
    metadata = json.loads(row["metadata"] or "{}")
    if metadata.get("turn_id") != turn_id:
        raise RuntimeError("Assistant Step belongs to a different Turn")
    if metadata.get("provider_turn_id") != provider_turn_id:
        raise RuntimeError("Assistant Step belongs to a different provider turn")
    calls = list(metadata.get("llm_tool_calls") or [])
    candidate = {
        "id": tool_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {}),
        },
    }
    matches = [call for call in calls if call.get("id") == tool_id]
    if matches:
        if len(matches) == 1 and matches[0] == candidate:
            return assistant_step_id
        raise RuntimeError("provider tool call identity changed within Assistant Step")
    calls.append(candidate)
    metadata["step_type"] = "tool_calls"
    metadata["llm_tool_calls"] = calls
    metadata["tool_hints"] = [
        *list(metadata.get("tool_hints") or []),
        hint or name,
    ]
    updated = db.execute(
        "UPDATE messages SET metadata=? WHERE id=? AND thread_id=? "
        "AND role='assistant' AND metadata=?",
        (
            json.dumps(metadata),
            assistant_step_id,
            child_thread_id,
            row["metadata"],
        ),
    )
    if updated.rowcount != 1:
        raise RuntimeError("Assistant Step tool-call append lost CAS ownership")
    return assistant_step_id
