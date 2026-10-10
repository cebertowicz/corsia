"""Resolve which Assistant Step owns each durable tool result.

Shared by the display projection (``core.message_display``, lenient) and the
model-history projection (``core.message_history``, strict).
"""

from __future__ import annotations

from collections import defaultdict

from core.message_row import assistant_calls, call_id, parse_meta
from loguru import logger


class ToolLineageError(RuntimeError):
    """A tool result claims authoritative lineage that durable rows disprove."""


def _is_synthetic_result(row: dict) -> bool:
    return parse_meta(row.get("metadata")).get("synthetic_tool_result") is True


def _prefer_result(current: dict | None, candidate: dict) -> dict:
    if current is None:
        return candidate
    current_synthetic = _is_synthetic_result(current)
    candidate_synthetic = _is_synthetic_result(candidate)
    if current_synthetic and not candidate_synthetic:
        return candidate
    if not current_synthetic and candidate_synthetic:
        return current
    # Duplicate provider delivery is resolved deterministically by durable order.
    return candidate


def _assistant_lineage_index(
    rows: list[dict],
) -> tuple[dict[str, tuple[int, dict]], list[tuple[int, dict]]]:
    """Index durable Assistant Steps once for display and mutation lineage."""
    assistant_by_id: dict[str, tuple[int, dict]] = {}
    assistants: list[tuple[int, dict]] = []
    for index, row in enumerate(rows):
        if row.get("role") != "assistant":
            continue
        assistants.append((index, row))
        step_id = row.get("id")
        if isinstance(step_id, str):
            if step_id in assistant_by_id:
                raise ToolLineageError(f"ambiguous assistant step id {step_id!r}")
            assistant_by_id[step_id] = (index, row)
    return assistant_by_id, assistants


def _resolve_tool_owner(
    row: dict,
    result_index: int,
    assistant_by_id: dict[str, tuple[int, dict]],
    assistants: list[tuple[int, dict]],
    *,
    require_preceding_authoritative: bool,
) -> dict | None:
    """Resolve one durable tool row without collapsing duplicate results."""
    meta = parse_meta(row.get("metadata"))
    has_authoritative_step = "assistant_step_id" in meta
    authoritative_step_id = meta.get("assistant_step_id")
    tool_call_id = row.get("tool_call_id") or meta.get("tool_call_id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        if has_authoritative_step:
            raise ToolLineageError("tool result has an invalid tool_call_id")
        return None

    target: dict | None = None
    if has_authoritative_step:
        if not isinstance(authoritative_step_id, str) or not authoritative_step_id:
            raise ToolLineageError("tool result has an invalid assistant_step_id")
        target_entry = assistant_by_id.get(authoritative_step_id)
        if target_entry is None:
            raise ToolLineageError(
                f"tool result names missing assistant step {authoritative_step_id!r}"
            )
        if require_preceding_authoritative and target_entry[0] >= result_index:
            raise ToolLineageError(
                f"tool result precedes assistant step {authoritative_step_id!r}"
            )
        target = target_entry[1]
        target_thread_id = target.get("thread_id")
        result_thread_id = row.get("thread_id")
        if (
            isinstance(target_thread_id, str)
            and isinstance(result_thread_id, str)
            and target_thread_id != result_thread_id
        ):
            raise ToolLineageError(
                f"cross-thread tool lineage for assistant step "
                f"{authoritative_step_id!r}"
            )
        matches = [
            call for call in assistant_calls(target)
            if call_id(call) == tool_call_id
        ]
        if not matches:
            raise ToolLineageError(
                f"assistant step {authoritative_step_id!r} does not declare "
                f"tool call {tool_call_id!r}"
            )
        if len(matches) != 1:
            raise ToolLineageError(
                f"ambiguous tool call {tool_call_id!r} in assistant step "
                f"{authoritative_step_id!r}"
            )
    else:
        candidates: list[tuple[dict, int]] = []
        for index, candidate in assistants:
            if index >= result_index:
                continue
            match_count = sum(
                call_id(call) == tool_call_id
                for call in assistant_calls(candidate)
            )
            if match_count:
                candidates.append((candidate, match_count))
        if not candidates:
            return None
        target = candidates[-1][0]

    step_id = target.get("id") if target is not None else None
    if not isinstance(step_id, str) or not step_id:
        return None
    return target


def resolve_tool_lineage(
    rows: list[dict], *, lenient: bool = False,
) -> dict[str, dict[str, dict]]:
    """Resolve display winners for each emitting Assistant Step and tool call.

    ``lenient`` is the display projection's mode: display never fails a
    thread over one row, so a tool result the lineage rules reject — a
    provisional result awaiting its provider call id, a quarantined unbound
    result, a malformed legacy row — renders unattached rather than raising.
    Model replay (``db_to_llm_history``) stays strict: silently dropping a
    real result from the model's context would replace durable truth with a
    fabricated interruption stub, so replay keeps the loud failure. The
    strict replay path keeps its fail-closed resolution.
    """
    assistant_by_id, assistants = _assistant_lineage_index(rows)

    resolved: dict[str, dict[str, dict]] = defaultdict(dict)
    for result_index, row in enumerate(rows):
        if row.get("role") != "tool":
            continue
        meta = parse_meta(row.get("metadata"))
        tool_call_id = row.get("tool_call_id") or meta.get("tool_call_id")
        try:
            target = _resolve_tool_owner(
                row,
                result_index,
                assistant_by_id,
                assistants,
                require_preceding_authoritative=False,
            )
        except ToolLineageError as error:
            if not lenient:
                raise
            logger.warning(
                "display lineage skipped tool result {} in session {}: {}",
                row.get("id"),
                row.get("thread_id"),
                error,
            )
            continue
        if target is None or not isinstance(tool_call_id, str) or not tool_call_id:
            continue
        step_id = target["id"]
        resolved[step_id][tool_call_id] = _prefer_result(
            resolved[step_id].get(tool_call_id), row,
        )
    return dict(resolved)
