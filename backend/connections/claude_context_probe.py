"""Reading a Claude Code context-usage probe's control responses.

The probe itself is spawned by ``harness_models``; this module owns the part
that has to explain itself. A refused control request is the only place the
harness says *why* it refused, so every failure here names the reason rather
than the selector alone (#1911, #2079). All of this text arrives from a
subprocess, so every rendered field is length-bounded.
"""

from __future__ import annotations

import json
from typing import Any

_CONTROL_DETAIL_MAX = 300
_SEEN_REQUEST_IDS_MAX = 20


def bounded_detail(value: str) -> str:
    """Trim harness-authored text to a length a log line can carry."""
    text = value.strip()
    if len(text) <= _CONTROL_DETAIL_MAX:
        return text
    return text[:_CONTROL_DETAIL_MAX] + "…"


def control_error_detail(response: dict[str, Any]) -> str:
    """Render the harness's own refusal detail from a control response."""
    subtype = bounded_detail(str(response.get("subtype") or "")) or "missing"
    parts = [f"subtype={subtype}"]
    for key in ("error", "message", "detail"):
        value = response.get(key)
        # An empty container is not a reason; fall through to the next key
        # rather than rendering a misleading "error={}".
        if isinstance(value, (dict, list)) and not value:
            continue
        if isinstance(value, dict):
            value = value.get("message") or json.dumps(value, sort_keys=True)
        text = "" if value is None else bounded_detail(str(value))
        if text:
            parts.append(f"{key}={text}")
            break
    return ", ".join(parts)


def read_context_usage(stdout: bytes, selector: str) -> dict[str, Any]:
    """Return the usage payload a probe's control responses carry.

    Raises ``RuntimeError`` naming the harness's own reason when the probe was
    refused or never produced usage at all.
    """
    seen_request_ids: list[str] = []
    for line in stdout.splitlines():
        try:
            response = json.loads(line).get("response") or {}
        except (json.JSONDecodeError, AttributeError):
            continue
        request_id = response.get("request_id")
        if request_id:
            text = bounded_detail(str(request_id))
            if text not in seen_request_ids and len(seen_request_ids) < _SEEN_REQUEST_IDS_MAX:
                seen_request_ids.append(text)
        if request_id == "corsia-set-model":
            if response.get("subtype") != "success":
                raise RuntimeError(
                    f"Claude Code rejected selector {selector}: "
                    f"{control_error_detail(response)}"
                )
        elif request_id == "corsia-context-usage":
            return _usage_from_response(response, selector)
    # Naming the parsed-line count separates "the harness said nothing" from
    # "it answered, but nothing correlated" — a different defect entirely.
    raise RuntimeError(
        f"Claude Code returned no context evidence for {selector}: "
        f"control responses seen: {', '.join(seen_request_ids) or 'none'} "
        f"({len(stdout.splitlines())} output lines)"
    )


def _usage_from_response(response: dict[str, Any], selector: str) -> dict[str, Any]:
    """Validate one context-usage response, naming each way it can be wrong."""
    if response.get("subtype") != "success":
        raise RuntimeError(
            f"Claude Code returned invalid context evidence for {selector}: "
            f"{control_error_detail(response)}"
        )
    usage = response.get("response")
    if isinstance(usage, dict):
        return usage
    raise RuntimeError(
        f"Claude Code returned invalid context evidence for {selector}: "
        f"usage payload was {type(usage).__name__}, expected object"
    )
