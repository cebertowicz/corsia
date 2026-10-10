"""Thread history — context rebuild and history preparation.

Pruning helpers are in message_pruning.py.
Token estimation is in token_estimation.py.
Thread auto-naming is in thread_naming.py.
"""

from __future__ import annotations

from infra import database as _database

from core.message_pruning import (  # noqa: F401
    PRUNE_PROTECT_COUNT,
    PRUNE_MIN_SIZE,
    PRUNE_CAPS,
    PRUNE_CAP_DEFAULT,
    _make_prune_stub,
    _find_superseded_indices,
    prune_old_tool_results,
)
from core.token_estimation import (  # noqa: F401
    DEFAULT_MEMORY_WINDOW,
    _estimate_tokens,
    _estimate_message_tokens,
    _estimate_messages_tokens,
    compute_memory_window,
    _sample_tool_ratio,
)
from core.thread_naming import (  # noqa: F401
    _clean_title,
    _call_title_llm,
    generate_thread_title,
)


def get_db():
    """Resolve the active database seam at call time."""
    return _database.get_db()


def _prepare_rebuilt_messages(
    row_dicts: list[dict],
    memory_window: int | None,
    *,
    exclude_current_user_text: str | None = None,
) -> list[dict]:
    from core.message_history import db_to_llm_history

    if exclude_current_user_text is not None and row_dicts:
        last = row_dicts[-1]
        if (
            last.get("role") == "user"
            and (last.get("content") or "") == exclude_current_user_text
        ):
            row_dicts = row_dicts[:-1]

    messages = db_to_llm_history(row_dicts)

    messages = prepare_history_for_llm(messages)

    messages = prune_old_tool_results(messages)

    if memory_window is not None and len(messages) > memory_window:
        messages = _trim_to_window(messages, memory_window)

    return messages


def rebuild_messages_from_db(
    thread_id: str,
    memory_window: int | None = 100,
) -> list[dict]:
    """Convert CORSIA DB messages -> LLM-format messages.

    Post-processes: merge consecutive assistants, fix tool pairing, prune old
    tool results, and optionally trim to a message window. ``None`` preserves
    the complete history for exact-budget gates. Stored activity summaries are
    searchable data, not synthetic conversation history.
    """
    from loguru import logger

    from core.tool_lineage import ToolLineageError

    db = get_db()
    rows = db.execute(
        "SELECT id, thread_id, role, content, metadata, created_at, seq, rowid "
        "FROM messages WHERE thread_id = ? AND delivery_state = 'delivered' "
        # A queued user row retains its original creation timestamp but gains
        # transcript sequence only when provider delivery is proved. Sequence
        # is therefore the causal order; startup rejects delivered rows with
        # no sequence rather than silently replaying corrupt history.
        "ORDER BY seq ASC",
        (thread_id,)
    ).fetchall()
    db.close()

    row_dicts = [dict(row) for row in rows]
    try:
        return _prepare_rebuilt_messages(
            row_dicts,
            memory_window,
        )
    except ToolLineageError as lineage_error:
        # A crash between canonical tool persistence and provider echo binding
        # strands a provisional row that strict replay rejects forever. Repair
        # is a durable quarantine, applied at most once; anything else stays
        # loud.
        repaired = repair_stranded_tool_lineage(thread_id)
        if not repaired:
            raise
        logger.warning(
            "replay lineage quarantined {} stranded tool result(s) for {}: {}",
            repaired,
            thread_id,
            lineage_error,
        )
    db = get_db()
    rows = db.execute(
        "SELECT id, thread_id, role, content, metadata, created_at, seq, rowid "
        "FROM messages WHERE thread_id = ? AND delivery_state = 'delivered' "
        "ORDER BY seq ASC",
        (thread_id,)
    ).fetchall()
    db.close()
    return _prepare_rebuilt_messages(
        [dict(row) for row in rows],
        memory_window,
    )


def repair_stranded_tool_lineage(thread_id: str) -> int:
    """Quarantine crash-stranded provisional tool rows so replay can rebuild.

    Returns how many rows were durably quarantined. Zero means the lineage
    failure was not this crash shape and must stay loud.
    """
    from core.tool_result_binding import quarantine_stranded_tool_results

    with _database.write_transaction(None, scope="repair_stranded_tool_lineage") as db:
        repaired = quarantine_stranded_tool_results(
            thread_id=thread_id, db=db,
        )
        return repaired


def rebuild_messages_from_connection(
    thread_id: str,
    memory_window: int,
    *,
    db,
    exclude_current_user_text: str | None = None,
) -> list[dict]:
    """Convert CORSIA DB rows using a caller-owned connection."""
    rows = db.execute(
        "SELECT id, thread_id, role, content, metadata, created_at, seq, rowid "
        "FROM messages "
        "WHERE thread_id = ? AND delivery_state = 'delivered' "
        "ORDER BY seq ASC",
        (thread_id,),
    ).fetchall()
    row_dicts = [dict(row) for row in rows]
    return _prepare_rebuilt_messages(
        row_dicts,
        memory_window,
        exclude_current_user_text=exclude_current_user_text,
    )


def _merge_consecutive_assistants(messages: list[dict]) -> list[dict]:
    """Merge adjacent text-only assistant fragments.

    Tool-bearing rows are durable Assistant-Step boundaries. Crossing either
    side of one would move later text or calls into the wrong step.
    """
    if not messages:
        return messages

    merged: list[dict] = []

    for msg in messages:
        if not merged or msg["role"] != "assistant":
            merged.append(msg)
            continue

        prev = merged[-1]
        if prev["role"] != "assistant":
            merged.append(msg)
            continue
        if prev.get("tool_calls") or msg.get("tool_calls"):
            merged.append(msg)
            continue

        prev_content = prev.get("content") or ""
        curr_content = msg.get("content") or ""
        if prev_content and curr_content:
            prev["content"] = prev_content + "\n\n" + curr_content
        elif curr_content:
            prev["content"] = curr_content

        prev_tc = prev.get("tool_calls", [])
        curr_tc = msg.get("tool_calls", [])
        if curr_tc:
            prev["tool_calls"] = prev_tc + curr_tc

    return merged


def _trim_to_window(messages: list[dict], window: int) -> list[dict]:
    """Trim messages to window size, respecting tool_call/result boundaries.

    Returns a contiguous suffix of ``messages`` whose first message is a
    ``user``-role anchor. The naive last-N slice can land entirely inside
    long assistant/tool-only run; in that case we expand the window backward
    until we hit a user message.

    Invariants preserved:
    - The returned list always starts with a user-role message when ``messages``
      contains at least one user-role entry.
    - Trailing assistant-with-tool-calls pairs are stripped if no tool_results
      follow, to avoid handing the LLM a dangling tool_call.
    - When ``messages`` has zero user-role entries (synthetic / corrupted
      session), returns ``[]`` — caller can detect and handle.
    """
    if not messages:
        return []

    candidate = messages[-window:]

    user_idx_in_candidate = next(
        (i for i, m in enumerate(candidate) if m.get("role") == "user"),
        None,
    )

    if user_idx_in_candidate is not None:
        trimmed = candidate[user_idx_in_candidate:]
    else:
        anchor = next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
            None,
        )
        if anchor is None:
            return []
        trimmed = messages[anchor:]

    while trimmed and trimmed[-1].get("role") == "assistant" and trimmed[-1].get("tool_calls"):
        trimmed.pop()

    return trimmed


def prepare_history_for_llm(messages: list[dict]) -> list[dict]:
    """Merge text fragments and repair tool pairing without crossing steps."""
    messages = _merge_consecutive_assistants(messages)
    messages = _fix_tool_pairing(messages)
    return messages


def _fix_tool_pairing(messages: list[dict]) -> list[dict]:
    """Fix tool_call/tool_result pairing issues.

    - Assistant with tool_calls but missing results → add synthetic "[interrupted]" results
    - Tool results without matching assistant tool_calls → drop
    - Tool results that don't match preceding assistant → drop
    """
    if not messages:
        return messages

    result: list[dict] = []
    i = 0
    while i < len(messages):
        msg = messages[i]

        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tc_ids = {tc["id"] for tc in msg["tool_calls"] if isinstance(tc, dict) and "id" in tc}
            tool_msgs = []
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                tool_msgs.append(messages[j])
                j += 1

            found_ids = {tm["tool_call_id"] for tm in tool_msgs
                         if tm.get("tool_call_id") in tc_ids}
            missing_ids = tc_ids - found_ids

            result.append(msg)

            for tm in tool_msgs:
                if tm.get("tool_call_id") in tc_ids:
                    result.append(tm)

            if missing_ids:
                for mid in missing_ids:
                    name = "unknown"
                    for tc in msg["tool_calls"]:
                        if isinstance(tc, dict) and tc.get("id") == mid:
                            func = tc.get("function", {})
                            name = func.get("name", "unknown")
                            break
                    result.append({
                        "role": "tool",
                        "tool_call_id": mid,
                        "name": name,
                        "content": "[Tool execution was interrupted]",
                    })

            i = j
        elif msg.get("role") == "tool":
            i += 1
        else:
            result.append(msg)
            i += 1

    return result
