"""Normalize raw Claude Code stream-json events into CORSIA events."""

from __future__ import annotations

from typing import cast

from subagents.cc_event_messages import from_assistant, from_stream_event, from_user


def _first_present(raw: dict, *keys: str):
    return next((raw[key] for key in keys if key in raw), None)


def _system_event(raw: dict) -> list[dict]:
    subtype = raw.get("subtype")
    if subtype == "init":
        return [{
            "kind": "init", "session_id": raw.get("session_id"),
            "model": raw.get("model"), "tools": raw.get("tools") or [],
            "mcp_servers": raw.get("mcp_servers") or [],
            "plugins": raw.get("plugins") or [], "agents": raw.get("agents") or [],
            "skills": raw.get("skills") or [],
            "slash_commands": _first_present(
                raw, "slash_commands", "slashCommands", "commands"
            ) or [],
            "cwd": raw.get("cwd"),
        }]
    if subtype == "status":
        return [{"kind": "status", "status": raw.get("status")}]
    if subtype in {"hook_started", "hook_response"}:
        return [{"kind": "tool_boundary_violation", "capability": "hook"}]
    return []


def _user_event(raw: dict) -> list[dict]:
    return from_user(raw.get("message") or {})


def _rate_limit_event(raw: dict) -> list[dict]:
    return [{"kind": "rate_limit", "rate_limit_info": raw.get("rate_limit_info")}]


def _result_event(raw: dict) -> list[dict]:
    return [{
        "kind": "result", "result": raw.get("result"),
        "is_error": bool(raw.get("is_error")),
        "cost_usd": raw.get("total_cost_usd"), "num_turns": raw.get("num_turns"),
        "stop_reason": raw.get("stop_reason"), "aggregate_usage": raw.get("usage"),
        "model_usage": raw.get("modelUsage"),
    }]


def _stream_event(raw: dict) -> list[dict]:
    events = from_stream_event(raw.get("event") or {})
    if raw.get("parent_tool_use_id"):
        # A subagent's own stream: its tool input is not this thread's to preview.
        return [event for event in events if not event["kind"].startswith("tool_input_")]
    return events


def normalize_event(raw: dict) -> list[dict]:
    """Translate one raw event; malformed or unknown events produce no output."""
    if not isinstance(raw, dict):
        return []
    handlers = {
        "system": _system_event,
        "stream_event": _stream_event,
        "assistant": lambda event: from_assistant(event.get("message") or {}),
        "user": _user_event,
        "result": _result_event,
        "rate_limit_event": _rate_limit_event,
    }
    handler = handlers.get(cast(str, raw.get("type")))
    return handler(raw) if handler is not None else []
