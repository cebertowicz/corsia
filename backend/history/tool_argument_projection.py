"""Bound provider-visible tool arguments only in checkpoint-tail replay."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.token_estimation import _estimate_tokens
from history.types import PortableHistoryError


def project_checkpoint_tool_call(
    message_id: str,
    call: dict[str, Any],
    budget_tokens: int | None,
) -> dict[str, Any]:
    """Return an exact call or one bounded argument-identity preview."""
    encoded = json.dumps(call, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if budget_tokens is None or _estimate_tokens(encoded) <= budget_tokens:
        return dict(call)
    if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or budget_tokens <= 0:
        raise PortableHistoryError("tool-argument preview requires a positive budget")
    function = call.get("function")
    if not isinstance(function, dict):
        raise PortableHistoryError("projected tool call has no function object")
    arguments = function.get("arguments", "")
    if not isinstance(arguments, str):
        arguments = json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    raw = arguments.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()

    def candidate(kept: int) -> str:
        head = kept // 2
        tail = kept - head
        suffix = arguments[-tail:] if tail else ""
        retained = arguments[:head] + suffix
        preview = {
            "_corsia_checkpoint_argument_preview": {
                "message_id": message_id,
                "arguments_sha256": digest,
                "arguments_byte_count": len(raw),
                "arguments_omitted_byte_count": max(
                    0,
                    len(raw) - len(retained.encode("utf-8")),
                ),
                "text": (
                    arguments[:head]
                    + "\n[…argument bytes omitted…]\n"
                    + suffix
                ),
            }
        }
        projected = dict(call)
        projected["function"] = {
            **function,
            "arguments": json.dumps(
                preview,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        }
        return json.dumps(
            projected, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    best = candidate(0)
    if _estimate_tokens(best) > budget_tokens:
        raise PortableHistoryError("tool-argument identity does not fit its budget")
    low, high = 1, len(arguments)
    while low <= high:
        kept = (low + high) // 2
        rendered = candidate(kept)
        if _estimate_tokens(rendered) <= budget_tokens:
            best, low = rendered, kept + 1
        else:
            high = kept - 1
    projected = json.loads(best)
    if not isinstance(projected, dict):  # pragma: no cover - encoded above
        raise PortableHistoryError("projected tool call is not an object")
    return projected


def render_checkpoint_tool_call(
    message_id: str,
    call: dict[str, Any],
    budget_tokens: int | None,
) -> str:
    """Render the checkpoint-source form of a projected tool call."""
    return json.dumps(
        project_checkpoint_tool_call(message_id, call, budget_tokens),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
