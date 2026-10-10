"""Display projection: durable rows to display Assistant Steps."""

from __future__ import annotations

import json
from typing import Any

from core.message_row import assistant_calls, call_id, parse_meta
from core.tool_lineage import ToolLineageError, resolve_tool_lineage
from loguru import logger
from tools.tool_display import settled_display_call


def _display_tool_call(call: dict) -> dict:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return {
        "id": call_id(call),
        "name": function.get("name", call.get("name", "")),
        "hint": call.get("hint") or function.get("name") or call.get("name", ""),
        "args": call.get("args", function.get("arguments", "")),
        "result": call.get("result"),
    }


def _convert_assistant_for_display(
    row: dict,
    results: dict[str, dict],
) -> dict:
    meta = parse_meta(row.get("metadata"))
    source_calls = meta.pop("llm_tool_calls", None) or meta.get("tool_calls") or []
    display_calls: list[dict] = []
    for source in source_calls:
        if not isinstance(source, dict):
            continue
        call = _display_tool_call(source)
        result_row = results.get(call["id"])
        if result_row is None:
            display_calls.append(settled_display_call(call, None, None))
            continue
        display_calls.append(settled_display_call(
            call,
            result_row.get("content", ""),
            parse_meta(result_row.get("metadata")),
        ))
    if display_calls:
        meta["tool_calls"] = display_calls
    else:
        meta.pop("tool_calls", None)
    copy = dict(row)
    copy["metadata"] = json.dumps(meta) if meta else row.get("metadata")
    return copy


def durable_row_text(content: str | None, metadata: Any) -> str:
    """What one durable row says, including a Step whose output was tool calls.

    A tool-calling Assistant Step carries empty ``content`` — its words live in
    the call arguments. A projection that reads ``content`` alone renders it as
    a blank line, which is the transcript claiming the model answered nothing.
    """
    if content:
        return content
    calls = assistant_calls({"metadata": metadata})
    return " · ".join(
        f"{call['name']}({call['args']})"
        for call in map(_display_tool_call, calls)
    )


def llm_to_display(rows: list[dict]) -> list[dict]:
    """Project durable rows into display Assistant Steps.

    Tool-result children never become display rows. They attach to the assistant
    row named by durable lineage regardless of intervening user or assistant
    rows, so an Assistant Step is stable across live delivery and reload.
    """
    try:
        lineage = resolve_tool_lineage(rows, lenient=True)
    except ToolLineageError as error:
        # Second layer of the same guarantee: even an index-level lineage
        # anomaly (for example a duplicate Assistant Step id) must degrade to
        # assistants-without-results, never to an unopenable thread.
        logger.warning(
            "display lineage disabled for session {}: {}",
            next((row.get("thread_id") for row in rows), None),
            error,
        )
        lineage = {}
    display: list[dict] = []
    for row in rows:
        role = row.get("role", "")
        if role == "tool":
            continue
        if role == "system" and parse_meta(row.get("metadata")).get("type") == "result":
            continue
        if role == "assistant":
            step_id = row.get("id")
            display.append(_convert_assistant_for_display(
                row,
                lineage.get(step_id, {}) if isinstance(step_id, str) else {},
            ))
        else:
            display.append(row)
    return display
