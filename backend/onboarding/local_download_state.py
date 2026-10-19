"""Safe, live local-model state shared by setup and the first-run wake."""

from __future__ import annotations

from typing import Any


_SAFE_CAUSES = frozenset({"connection", "disk_full", "other", "memory", "other_omlx"})
_LIVE_STATES = frozenset({
    "ineligible_memory", "pick_failed", "downloading", "stopped", "failed", "starting",
    "not_loaded", "start_failed", "ready",
})


def normalize(status: dict[str, Any]) -> dict[str, Any]:
    """Keep only the action-relevant local state; installer text and credentials never escape."""
    state = status.get("state")
    if state in _LIVE_STATES:
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
        elif state in {"pick_failed", "failed", "start_failed", "not_loaded"}:
            # Local ownership reports an exception class. The wake contract carries
            # a stable, actionable category rather than that implementation detail.
            label = str(cause or "").lower()
            if "disk" in label or "space" in label:
                detail["cause"] = "disk_full"
            elif "connect" in label or "network" in label:
                detail["cause"] = "connection"
            elif "memory" in label or "ram" in label:
                detail["cause"] = "memory"
            elif "foreign" in label or "omlx" in label:
                detail["cause"] = "other_omlx"
            else:
                detail["cause"] = "other"
        bytes_to_free = status.get("bytes_to_free")
        if isinstance(bytes_to_free, int) and not isinstance(bytes_to_free, bool) and bytes_to_free > 0:
            detail["bytes_to_free"] = bytes_to_free
        ref = status.get("ref")
        if isinstance(ref, str) and ref and len(ref) <= 160:
            detail["ref"] = ref
        return detail
    # Compatibility with the pre-redesign local-flight owner until it exposes the live contract above.
    if state == "running":
        return {"state": "starting", "progress": status.get("stage")}
    if state == "succeeded":
        # A completed download is not usable until the catalog reports a ready local model.
        return {"state": "not_loaded"}
    if state == "failed":
        return {"state": "failed", "cause": "other"}
    return {"state": "stopped"}


def current() -> dict[str, Any]:
    """Read the current setup-flight state once through the canonical local owner."""
    from llm.local_setup import setup_status

    return normalize(setup_status())
