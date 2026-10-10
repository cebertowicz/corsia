"""Pure harness usage version normalization; harness_usage owns account storage.

Version 2 keys a Codex by-limit bucket by both meter and window. Version 1
cannot identify the plan meter, so drops every by-limit window on read.
Version 3 (G10) drops old Claude event fractions instead of guessing their unit,
and drops discovery windows keyed by a raw vendor scope instead of their tier.
Reads normalize without persisting; the next canonical write stores version 3.
"""
from __future__ import annotations

from typing import Any

from connections.usage_verdicts import ACCOUNT_SCOPE, window_scope
from connections.usage_windows import window_key, without_reading

USAGE_VERSION = 3
_CLAUDE = "claude-code"


def empty_usage() -> dict[str, Any]:
    return {"version": USAGE_VERSION, "windows": {}, "facts": {}}


def claude_tier(kind: object) -> str | None:
    """The scope a Claude window's kind names: a tier, or None for the account or a non-plan window."""
    scope = window_scope(kind)
    return None if scope in (None, ACCOUNT_SCOPE) else scope


def _current_window(window: dict[str, Any]) -> dict[str, Any] | None:
    if window.get("source") == "rate_limit_event":
        window = without_reading(window)
    if window.get("vendor") == _CLAUDE and window_scope(window.get("kind")) is not None:
        canonical = {**window, "scope_model": claude_tier(window.get("kind"))}
        if window_key(canonical) != window_key(window):
            return None
    return window


def current(usage: dict[str, Any]) -> dict[str, Any]:
    """The blob at ``USAGE_VERSION``; an older blob loses only what it cannot vouch for, one step per version."""
    version = usage.get("version")
    if version == USAGE_VERSION:
        return usage
    stored = usage.get("windows")
    windows = {
        key: kept for key, window in (stored.items() if isinstance(stored, dict) else ())
        if isinstance(window, dict) and not (version != 2 and window.get("limit_id"))
        and (kept := _current_window(window)) is not None
    }
    return {**usage, "version": USAGE_VERSION, "windows": windows}
