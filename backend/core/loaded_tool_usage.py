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


def _failed(content: str, is_error: bool = False) -> bool:
    """One verdict for a failed lookup, whether it arrives as a message or a stored row."""
    return is_error or content.startswith("Error:")


def _server_lookup(arguments: dict[str, Any]) -> str | None:
    """The app a ``server=`` lookup named, taken from the call so a removed app keeps its row."""
    server = arguments.get("server")
    return server if isinstance(server, str) and server else None


def _schemas(content: str) -> list[tuple[str, str]]:
    """``(tool name, schema text)`` for each schema a ``names``/``pack`` result carries.

    The result is a header line, one JSON list of schemas, then any
    unknown-name error; only the list's schemas are tool definitions.
    """
    _header, _, rest = content.partition("\n")
    try:
        schemas = json.loads(rest.split("\n", 1)[0])
    except ValueError:
        return []
    if not isinstance(schemas, list):
        return []
    return [
        (schema["name"], json.dumps(schema, ensure_ascii=False))
        for schema in schemas
        if isinstance(schema, dict) and isinstance(schema.get("name"), str)
    ]


def _shares(
    arguments: dict[str, Any], content: str, tokens: int,
    tool_server: dict[str, str], model: str | None,
) -> dict[str, int]:
    """One successful lookup's tokens per app; CORSIA schemas and framing stay in History."""
    server = _server_lookup(arguments)
    if server is not None:
        return {server: tokens}
    measured: dict[str, int] = {}
    for name, text in _schemas(content):
        owner = tool_server.get(normalize_tool_name(name))
        if owner:
            measured[owner] = measured.get(owner, 0) + _text_tokens(text, model)
    attributed = sum(measured.values())
    if attributed <= tokens:
        return measured
    # Separately measured pieces can sum past the whole result; never exceed it.
    return {owner: share * tokens // attributed for owner, share in measured.items()}


def _attribute(
    results: Iterable[tuple[dict[str, Any], str, int]],
    tool_server: dict[str, str], model: str | None,
) -> list[dict[str, Any]]:
    """Total ``(arguments, content, tokens)`` lookups per app, largest first."""
    totals: dict[str, int] = {}
    for arguments, content, tokens in results:
        for server, share in _shares(arguments, content, tokens, tool_server, model).items():
            totals[server] = totals.get(server, 0) + share
    return [
        {"server": server, "display_name": app_display_name(server), "tokens": tokens}
        for server, tokens in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
        if tokens > 0
    ]


def _message_lookups(history: list[dict[str, Any]], model: str | None):
    """Successful ``tools_load`` results in a retained message list, paired to their calls."""
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
        if (
            message.get("role") == "tool" and arguments is not None
            and isinstance(content, str) and not _failed(content)
        ):
            yield arguments, content, _estimate_message_tokens(message, model=model)


def _row_lookups(rows: Iterable[Any], model: str | None):
    """Successful ``tools_load`` results among stored rows, measured as the preview measures a row."""
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
        if (
            arguments is not None and isinstance(content, str)
            and not _failed(content, metadata.get("is_error") is True)
        ):
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
    server; a ``names``/``pack`` result is split schema by schema, so CORSIA
    schemas and the result's framing stay plain History, while a ``server=``
    index listing belongs wholly to the app the call named.
    """
    tool_server = tool_server or {}
    if history:
        return _attribute(_message_lookups(history, model), tool_server, model)
    if rows:
        return _attribute(_row_lookups(rows, model), tool_server, model)
    return []
