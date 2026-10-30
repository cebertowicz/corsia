"""Safe, live local-model state shared by setup and the first-run wake."""

from __future__ import annotations

from typing import Any

from llm.local_setup import terminal_outcome
from llm.local_setup_results import CAUSES as _SAFE_CAUSES


def normalize(status: dict[str, Any]) -> dict[str, Any]:
    """Keep only the action-relevant local state; installer text and credentials never escape."""
    state = terminal_outcome({"state": status.get("state")})
    if state != "stopped":
        detail: dict[str, Any] = {"state": state}
        model = status.get("model")
        if isinstance(model, str) and model:
            detail["model"] = model[:512]
        percent = status.get("percent")
        if isinstance(percent, (int, float)) and not isinstance(percent, bool):
            detail["percent"] = max(0, min(100, percent))
        eta_seconds = status.get("eta_seconds")
        if isinstance(eta_seconds, int) and not isinstance(eta_seconds, bool) and eta_seconds >= 0:
            detail["eta_seconds"] = eta_seconds
        download_size = status.get("download_size")
        if isinstance(download_size, int) and not isinstance(download_size, bool) and download_size > 0:
            detail["download_size"] = download_size
        if status.get("stalled") is True:
            detail["stalled"] = True
        cause = status.get("cause")
        if cause in _SAFE_CAUSES:
            detail["cause"] = cause
        bytes_to_free = status.get("bytes_to_free")
        if isinstance(bytes_to_free, int) and not isinstance(bytes_to_free, bool) and bytes_to_free > 0:
            detail["bytes_to_free"] = bytes_to_free
        ref = status.get("ref")
        if isinstance(ref, str) and ref and len(ref) <= 160:
            detail["ref"] = ref
        catalog_line = status.get("catalog_line")
        if cause == "load_refused" and isinstance(catalog_line, str) and catalog_line:
            detail["catalog_line"] = catalog_line
        fix = status.get("fix")
        if isinstance(fix, str) and fix:
            detail["fix"] = fix[:512]
        return detail
    return {"state": "stopped"}


def current() -> dict[str, Any]:
    """Read the current setup-flight state once through the canonical local owner."""
    from llm.local_setup import setup_status

    return normalize(setup_status())
