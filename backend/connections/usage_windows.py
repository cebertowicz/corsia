"""One usage window's merge and staleness rules.

A window is keyed by vendor, meter, kind and scope, so one vendor meter is one
stored window whichever frame reported it. A newer frame replaces its fields;
a percentage keeps the observation time and reset of the window it was read
from, so it is never shown under a later window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def parse_time(value: object) -> datetime | None:
    """An ISO time as an aware datetime, or None when unreadable."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def window_key(window: dict[str, Any]) -> str | None:
    """The stored key of a window: vendor, by-limit meter if any, kind and scope."""
    kind = window.get("kind")
    if not isinstance(kind, str) or not kind:
        return None
    meter = [window["limit_id"]] if window.get("limit_id") else []
    parts = [window.get("vendor"), *meter, kind, window.get("scope_model") or "account"]
    return ":".join(str(part) for part in parts)


def _source_priority(source: object) -> int:
    return 2 if source in {"rate_limit_event", "rate_limit_update", "turn_error"} else 1


def newer(candidate: dict[str, Any], existing: dict[str, Any] | None) -> bool:
    """Whether a candidate observation replaces the existing one."""
    if existing is None:
        return True
    candidate_time = parse_time(candidate.get("observed_at"))
    existing_time = parse_time(existing.get("observed_at"))
    if candidate_time is None:
        return False
    if existing_time is None or candidate_time > existing_time:
        return True
    if candidate_time < existing_time:
        return False
    return _source_priority(candidate.get("source")) > _source_priority(existing.get("source"))


_PERCENT_FIELDS = ("used_percent", "used_percent_observed_at", "used_percent_resets_at")


def merged_window(current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Apply a newer frame; a percentage keeps the observation time and reset of its own window."""
    merged = dict(current)
    if "used_percent" in candidate:
        candidate["used_percent_observed_at"] = candidate["observed_at"]
        if "resets_at" in candidate:
            candidate["used_percent_resets_at"] = candidate["resets_at"]
        else:
            merged.pop("used_percent_resets_at", None)
            # A reset that passed before this reading cannot describe it.
            if _percent_reset_passed({"resets_at": merged.get("resets_at")}, candidate["observed_at"]):
                merged.pop("resets_at", None)
    elif "used_percent" in merged:
        merged.setdefault("used_percent_observed_at", merged.get("observed_at"))
        if "resets_at" in merged:
            merged.setdefault("used_percent_resets_at", merged["resets_at"])
        if _percent_reset_passed(merged, candidate["observed_at"]):
            for field in _PERCENT_FIELDS:
                merged.pop(field, None)
    merged.update(candidate)
    return merged


def without_reading(window: dict[str, Any]) -> dict[str, Any]:
    """The window with its percentage dropped: a reading that no longer describes the account."""
    return {key: value for key, value in window.items() if key not in _PERCENT_FIELDS}


def _percent_reset_passed(window: dict[str, Any], at: str) -> bool:
    """Whether the window the percentage was read from has reset by ``at``."""
    reset = parse_time(window.get("used_percent_resets_at", window.get("resets_at")))
    moment = parse_time(at)
    return reset is not None and moment is not None and reset <= moment


def window_stale(window: dict[str, Any], now: datetime) -> bool:
    """Whether a window's percentage can no longer be shown as current."""
    observed = parse_time(window.get("used_percent_observed_at", window.get("observed_at")))
    if observed is None or _percent_reset_passed(window, now.isoformat()):
        return True
    minutes = window_length(window)
    if minutes is not None:
        return observed + timedelta(minutes=minutes) <= now
    reset = parse_time(window.get("resets_at"))
    return reset is None or reset <= now


def window_length(window: dict[str, Any]) -> int | None:
    """A window's length in minutes: the vendor's stated length, else the one its kind names.

    Codex states ``window_minutes``; Claude names it in the kind (``five_hour``,
    ``seven_day`` and its ``seven_day_*`` tiers). Derived here, never stored.
    """
    minutes = window.get("window_minutes")
    if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0:
        return minutes
    kind = window.get("kind")
    if kind == "five_hour":
        return 300
    if kind == "seven_day" or (isinstance(kind, str) and kind.startswith("seven_day_")):
        return 10080
    return None
