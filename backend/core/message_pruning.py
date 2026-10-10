"""Message pruning for old tool results."""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from tools.tool_surface import TOOL_CALL, resolve_dispatch


from infra.constants import PRUNE_PROTECT_COUNT, PRUNE_MIN_SIZE, PRUNE_CAP_DEFAULT  # noqa: F401

PRUNE_CAPS = {
    "read_file": 500,
    "web_fetch": 300,
    "web_search": 200,
    "bash": 300,
    "grep": 200,
    "glob": 100,
    "list_dir": 100,
}


def _make_prune_stub(name: str, content: str) -> str:
    """Create a compact stub summarizing a pruned tool result."""
    lines = content.count("\n") + 1
    chars = len(content)

    hint = ""
    if name == "read_file":
        first_line = content.split("\n", 1)[0][:120]
        hint = f" — {first_line}"
    elif name in ("grep", "glob"):
        match_lines = [l for l in content.split("\n") if l.strip()]
        hint = f" — {len(match_lines)} results"
    elif name in ("web_fetch", "web_search"):
        first_line = content.split("\n", 1)[0][:100]
        hint = f" — {first_line}"
    elif name == "bash":
        first_line = content.split("\n", 1)[0][:80]
        hint = f" — {first_line}"

    return f"[{name}: {chars} chars, {lines} lines{hint} — pruned from history, re-read if needed]"


def _effective_calls(messages: list[dict]) -> dict[str, tuple[str, Any]]:
    """Map each assistant tool call id to the tool it ran and its arguments.

    A ``tool_call`` dispatch resolves to the tool it names, so per-tool
    pruning treats a dispatched ``web_search`` like a direct one.
    """
    calls: dict[str, tuple[str, Any]] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function", {})
            args = func.get("arguments", "")
            try:
                parsed = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                parsed = None
            name, arguments, _ = resolve_dispatch(func.get("name", ""), parsed)
            calls[tc.get("id", "")] = (name, arguments)
    return calls


def _tool_name(msg: dict, calls: dict[str, tuple[str, Any]], default: str) -> str:
    """Return the tool behind a result row, looking through a dispatch."""
    name = msg.get("name", default)
    call = calls.get(msg.get("tool_call_id", ""))
    return call[0] if name == TOOL_CALL and call else name


def _find_superseded_indices(
    messages: list[dict], calls: dict[str, tuple[str, Any]],
) -> set[int]:
    """Find tool result indices that are superseded by later tool calls."""
    superseded: set[int] = set()

    tool_info: list[tuple[int, str, str]] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
        name = _tool_name(msg, calls, "")
        content = msg.get("content", "")
        hint = content.split("\n", 1)[0][:200] if content else ""
        tool_info.append((i, name, hint))

    edit_paths = {
        arguments["path"] for name, arguments in calls.values()
        if name == "edit_file" and isinstance(arguments, dict)
        and arguments.get("path")
    }

    for idx, name, hint in tool_info:
        if name == "read_file" and edit_paths:
            for ep in edit_paths:
                if ep in hint:
                    superseded.add(idx)
                    break

    for j, (idx, name, hint) in enumerate(tool_info):
        if name == "web_search":
            for k in range(j + 1, min(j + 10, len(tool_info))):
                if tool_info[k][1] == "web_fetch":
                    superseded.add(idx)
                    break

    return superseded


def prune_old_tool_results(messages: list[dict]) -> list[dict]:
    """Replace old tool results with compact stubs to save context space."""
    tool_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool" and m.get("content")
    ]

    calls = _effective_calls(messages)
    superseded = _find_superseded_indices(messages, calls)

    age_prune = set()
    if len(tool_indices) > PRUNE_PROTECT_COUNT:
        age_prune = set(tool_indices[:-PRUNE_PROTECT_COUNT])

    prune_set = age_prune | superseded

    if not prune_set:
        return messages

    pruned = []
    pruned_count = 0
    saved_chars = 0

    for i, msg in enumerate(messages):
        if i in prune_set:
            content = msg.get("content", "")
            name = _tool_name(msg, calls, "tool")
            cap = PRUNE_CAPS.get(name, PRUNE_CAP_DEFAULT)
            if len(content) > max(PRUNE_MIN_SIZE, cap):
                stub = _make_prune_stub(name, content)
                pruned.append({**msg, "content": stub})
                pruned_count += 1
                saved_chars += len(content) - len(stub)
            else:
                pruned.append(msg)
        else:
            pruned.append(msg)

    if pruned_count:
        logger.info(
            "Pruned {} old tool results, saved ~{} chars (~{} tokens)",
            pruned_count, saved_chars, saved_chars // 3,
        )

    return pruned
