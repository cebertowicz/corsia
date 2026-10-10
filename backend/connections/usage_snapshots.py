"""Pure Codex snapshot normalization; the canonical usage writer owns stamping."""
from __future__ import annotations

from typing import Any

from connections.usage_verdicts import epoch_iso as _iso

CODEX_DEFAULT_LIMIT = "codex"


def reset_from_snapshot(snapshot: object) -> str | None:
    """Latest reset among reported exhausted Codex windows, never an unrelated one."""
    if not isinstance(snapshot, dict):
        return None
    resets = [
        reset
        for window in (snapshot.get("primary"), snapshot.get("secondary"))
        if isinstance(window, dict) and isinstance(window.get("usedPercent"), (int, float))
        and not isinstance(window.get("usedPercent"), bool) and window["usedPercent"] >= 100
        and (reset := _iso(window.get("resetsAt"))) is not None
    ]
    return max(resets) if resets else None


def merge_snapshot(previous: object, update: object) -> dict[str, Any]:
    """Merge a sparse Codex notification for session-local vendor context only."""
    merged = dict(previous) if isinstance(previous, dict) else {}
    if isinstance(update, dict):
        merged.update({key: value for key, value in update.items() if value is not None})
        if "rateLimitReachedType" in update:
            merged["rateLimitReachedType"] = update["rateLimitReachedType"]
    return merged


def codex_windows(snapshot: object, *, limit_id: str | None = None, source: str) -> list[dict[str, Any]]:
    """Normalize only the Codex windows actually present in one vendor frame.

    Each names its meter: the frame's ``limitId``, else its bucket key, else Codex's default.
    The merge stamps them, with the frame's verdict, at one instant.
    """
    if not isinstance(snapshot, dict):
        return []
    limit_id = snapshot.get("limitId") or limit_id or CODEX_DEFAULT_LIMIT
    windows: list[dict[str, Any]] = []
    for name in ("primary", "secondary"):
        raw = snapshot.get(name)
        if not isinstance(raw, dict) or not isinstance(raw.get("usedPercent"), int):
            continue
        windows.append({
            "vendor": "codex", "kind": f"codex_{name}", "limit_id": limit_id,
            "used_percent": raw["usedPercent"], "resets_at": _iso(raw.get("resetsAt")),
            "window_minutes": raw.get("windowDurationMins"), "source": source,
        })
    return windows
