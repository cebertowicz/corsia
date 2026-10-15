"""Estimated cost of tool definitions an agent looked up, by connected app.

``tools_load`` results are ordinary tool messages in the conversation, so their
tokens are already inside ``history_tokens``. This module only attributes them:
it reads the same history the preview measured and reports, per app, what its
looked-up definitions cost, measured the way History measured them. It
subtracts and adds nothing; the sheet carves the figures out of the History
row they came from.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from core.token_estimation import _estimate_message_tokens, _text_tokens
from tools.catalog import normalize_tool_name
from tools.tool_surface import app_display_name

_TOOLS_LOAD = "tools_load"


def _arguments(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def _requested_servers(arguments: dict[str, Any], tool_server: dict[str, str]) -> list[str]:
    """The apps one lookup reached: ``server=`` directly, ``names=`` via the live index."""
    server = arguments.get("server")
    if isinstance(server, str) and server in set(tool_server.values()):
        return [server]
    names = arguments.get("names")
    if not isinstance(names, list):
        return []
    found = (tool_server.get(normalize_tool_name(name)) for name in names if isinstance(name, str))
    return sorted({server for server in found if server})


def _split(total: int, servers: list[str]) -> dict[str, int]:
    """Share one result's tokens across the apps it reached, summing exactly."""
    base, extra = divmod(total, len(servers))
    return {server: base + (1 if index < extra else 0) for index, server in enumerate(servers)}


def _attribute(
    results: Iterable[tuple[dict[str, Any], str, int]], tool_server: dict[str, str],
) -> list[dict[str, Any]]:
    """Total ``(arguments, content, tokens)`` lookups per app, largest first."""
    totals: dict[str, int] = {}
    for arguments, content, tokens in results:
        if content.startswith("Error:"):
            continue
        servers = _requested_servers(arguments, tool_server)
        if not servers:
            continue
        for server, share in _split(tokens, servers).items():
            totals[server] = totals.get(server, 0) + share
    return [
        {"server": server, "display_name": app_display_name(server), "tokens": tokens}
        for server, tokens in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
        if tokens > 0
    ]


def _message_lookups(history: list[dict[str, Any]], model: str | None):
    """``tools_load`` results in a retained message list, paired to their calls."""
    requests: dict[str, dict[str, Any]] = {}
    for message in history:
        for call in message.get("tool_calls") or ():
            function = call.get("function") or {}
            arguments = _arguments(function.get("arguments"))
            if call.get("id") and arguments is not None and normalize_tool_name(function.get("name")) == _TOOLS_LOAD:
                requests[call["id"]] = arguments
    for message in history:
        arguments = requests.get(message.get("tool_call_id") or "")
        content = message.get("content")
        if message.get("role") == "tool" and arguments is not None and isinstance(content, str):
            yield arguments, content, _estimate_message_tokens(message, model=model)


def _row_lookups(rows: Iterable[Any], model: str | None):
    """``tools_load`` results among stored rows, measured as the preview measures a row."""
    for row in rows:
        if row["role"] != "tool":
            continue
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except ValueError:
            continue
        if normalize_tool_name(metadata.get("name")) != _TOOLS_LOAD:
            continue
        arguments, content = _arguments(metadata.get("arguments")), row["content"]
        if arguments is not None and isinstance(content, str) and metadata.get("is_error") is not True:
            yield arguments, content, _text_tokens(content, model) + 4


def loaded_tool_tokens(
    history: list[dict[str, Any]] | None = None,
    tool_server: dict[str, str] | None = None,
    *,
    rows: Iterable[Any] | None = None,
    model: str | None,
) -> list[dict[str, Any]]:
    """Per-app estimated tokens of successful ``tools_load`` results.

    Give either the retained message list the estimate measured or the stored
    rows it measured. ``tool_server`` maps a public MCP tool name to its
    server; results that reached no connected app (CORSIA packs, errors) stay
    plain History.
    """
    if not tool_server:
        return []
    if history:
        return _attribute(_message_lookups(history, model), tool_server)
    if rows:
        return _attribute(_row_lookups(rows, model), tool_server)
    return []
