from __future__ import annotations

from typing import Any

from inspect_ai.solver import TaskState



def get_response(state: TaskState) -> dict[str, Any]:
    resp = state.store.get("corsia_eval_response")
    return resp if isinstance(resp, dict) else {}


def get_events(state: TaskState) -> list[dict[str, Any]]:
    resp = get_response(state)
    explicit = resp.get("events")
    if isinstance(explicit, list):
        return [_normalize(e) for e in explicit if isinstance(e, dict)]
    return _derive_from_legacy(resp)


def get_history(state: TaskState) -> list[dict[str, Any]]:
    resp = get_response(state)
    explicit = resp.get("history")
    if isinstance(explicit, list):
        return [h for h in explicit if isinstance(h, dict)]

    derived: list[dict[str, Any]] = []
    user_text = _resolve_user_input(state)
    if user_text:
        derived.append({"role": "user", "content": user_text})
    assistant_text = resp.get("content")
    if isinstance(assistant_text, str) and assistant_text:
        derived.append({"role": "assistant", "content": assistant_text})
    for tc in resp.get("tool_calls", []) or []:
        if not isinstance(tc, dict):
            continue
        derived.append({
            "role": "tool",
            "name": tc.get("name") or "",
            "content": _stringify_args(tc.get("arguments")),
        })
    return derived


def get_turn_attrs(state: TaskState) -> list[dict[str, Any]]:
    raw = state.store.get("turn_attrs")
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, dict)]


def get_assistant_content(state: TaskState) -> str:
    resp = get_response(state)
    content = resp.get("content")
    return content if isinstance(content, str) else ""



_PROVIDER_ERROR_KINDS: frozenset[str] = frozenset({"error"})


def _normalize(ev: dict[str, Any]) -> dict[str, Any]:
    payload = ev.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    iteration_raw = ev.get("iteration", 0)
    try:
        iteration = int(iteration_raw)
    except (TypeError, ValueError):
        iteration = 0
    return {
        "kind": str(ev.get("kind") or ""),
        "iteration": iteration,
        "payload": dict(payload),
    }


def _derive_from_legacy(resp: dict[str, Any]) -> list[dict[str, Any]]:
    iteration_count = resp.get("iteration_count")
    last_iter = max(int(iteration_count) - 1, 0) if isinstance(iteration_count, int) else 0

    events: list[dict[str, Any]] = []

    content = resp.get("content")
    if isinstance(content, str) and content:
        events.append({
            "kind": "text",
            "iteration": last_iter,
            "payload": {"content": content},
        })

    tool_calls = resp.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        events.append({
            "kind": "tool_calls_start",
            "iteration": last_iter,
            "payload": {"tool_call_dicts": [
                {"function": {"name": tc.get("name"), "arguments": tc.get("arguments")},
                 "id": tc.get("id")}
                for tc in tool_calls if isinstance(tc, dict)
            ]},
        })
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            events.append({
                "kind": "tool_complete",
                "iteration": last_iter,
                "payload": {"name": tc.get("name"), "id": tc.get("id")},
            })

    err = resp.get("error")
    if isinstance(err, str) and err:
        events.append({
            "kind": "error",
            "iteration": last_iter,
            "payload": {"message": err},
        })

    spans = (resp.get("span_summary") or {}).get("spans") or []
    for sp in spans:
        if not isinstance(sp, dict):
            continue
        if sp.get("name") == "corsia.tool_call":
            start_ns = sp.get("start_ns") or 0
            end_ns = sp.get("end_ns") or start_ns
            latency_ms = round((end_ns - start_ns) / 1_000_000.0, 3)
            attrs = sp.get("attrs") or {}
            events.append({
                "kind": "tool_complete",
                "iteration": last_iter,
                "payload": {
                    "name": attrs.get("tool.name"),
                    "latency_ms": latency_ms,
                },
            })

    return events


def _resolve_user_input(state: TaskState) -> str:
    txt = getattr(state, "input_text", None)
    if isinstance(txt, str) and txt.strip():
        return txt
    inp = getattr(state, "input", None)
    if isinstance(inp, str) and inp.strip():
        return inp
    up = getattr(state, "user_prompt", None)
    if up is not None:
        text = getattr(up, "text", None)
        if isinstance(text, str) and text.strip():
            return text
    return ""


def _stringify_args(args: Any) -> str:
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    import json
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False)
    except TypeError:
        return str(args)


__all__ = [
    "get_response",
    "get_events",
    "get_history",
    "get_turn_attrs",
    "get_assistant_content",
]
