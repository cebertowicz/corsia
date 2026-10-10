"""Classify Codex rejection of an image embedded in resumed history."""

from __future__ import annotations

import re
from typing import Any

from subagents.codex_app_server_errors import CodexAppServerError

HISTORICAL_IMAGE_REJECTION = "historical_image_rejected"
_HISTORICAL_IMAGE_PATH = re.compile(
    r"\binput\[\d+\]\.output\[\d+\]\.image_url\b",
    re.IGNORECASE,
)
_ACCEPTED_TURN_STATES = frozenset({"active", "completed"})


def _provider_status(payload: dict[str, Any]) -> int | None:
    code = payload.get("code")
    if code == 400:
        return 400
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("httpStatusCode", "http_status_code", "statusCode", "status"):
        value = data.get(key)
        if value == 400:
            return 400
    return None


def _provider_turn_state(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    candidates: tuple[object, ...] = (
        payload.get("turn"),
        data.get("turn") if isinstance(data, dict) else None,
    )
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        state = candidate.get("status") or candidate.get("state")
        if isinstance(state, str) and state.strip():
            return state.strip().lower()
    return None


def classify_historical_image_rejection(
    error: BaseException,
    *,
    fresh_attachments: bool,
    provider_turn_state: str | None = None,
) -> str | None:
    """Return the recovery cause only for the exact deterministic 400 shape.

    The app-server request layer separately proves this was an explicit provider
    rejection rather than an ambiguous transport failure. Fresh input media and
    any accepted provider Turn remain outside this historical recovery path.
    """
    if not isinstance(error, CodexAppServerError) or fresh_attachments:
        return None
    if _provider_status(error.payload) != 400:
        return None
    observed_state = (
        provider_turn_state.strip().lower()
        if isinstance(provider_turn_state, str) and provider_turn_state.strip()
        else _provider_turn_state(error.payload)
    )
    if observed_state in _ACCEPTED_TURN_STATES:
        return None
    message = str(error.payload.get("message") or error)
    if "invalid" not in message.lower():
        return None
    if _HISTORICAL_IMAGE_PATH.search(message) is None:
        return None
    return HISTORICAL_IMAGE_REJECTION
