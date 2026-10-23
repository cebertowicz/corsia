"""Disposable-evaluation tool allowlist admission and receipt material."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable


_ENV = "CORSIA_EVAL_TOOL_ALLOWLIST"


def eval_tool_allowlist() -> tuple[str, ...] | None:
    """Return the exact test-only tool set, refusing production configuration."""
    raw = os.environ.get(_ENV)
    if raw is None:
        return None
    if os.environ.get("CORSIA_TEST_MODE") != "1":
        raise RuntimeError(f"{_ENV} is allowed only when CORSIA_TEST_MODE=1")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{_ENV} must be a JSON list of tool names") from exc
    if not isinstance(values, list) or not values or any(
        not isinstance(name, str) or not name for name in values
    ):
        raise RuntimeError(f"{_ENV} must be a non-empty JSON list of tool names")
    names = tuple(sorted(set(values)))
    if len(names) != len(values):
        raise RuntimeError(f"{_ENV} must not contain duplicate tool names")
    return names


def allowlist_hash(names: Iterable[str]) -> str:
    """Hash the canonical allowlist bytes recorded in the context receipt."""
    payload = json.dumps(sorted(names), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
