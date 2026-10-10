"""Shared provider subscription and quota exhaustion classification.

A hard account or plan limit should select the configured fallback immediately;
a transient provider throttle should remain eligible for bounded retry.  The
marker set is used by both structured inference and reviewer harnesses.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


#: The vendors' own wording: Claude "resets 10:30pm" and "resets Sep 30 at 10pm",
#: Codex "try again at Oct 1st, 2026 2:32 AM". Month, day and year are optional so a
#: bare clock time keeps its next-occurrence reading.
_LOCAL_RESET = re.compile(
    r"\b(?:resets?|try again at)\s+"
    r"(?:(?P<month>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s*(?P<year>\d{4}))?,?\s+(?:at\s+)?|at\s+)?"
    r"(?P<hour>1[0-2]|[1-9])(?::(?P<minute>[0-5]\d))?\s*(?P<meridiem>[ap])\.?m\.?\b",
    re.IGNORECASE,
)
_MONTHS = {
    name: number for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1,
    )
}
#: A zone stated right after the reset time: an IANA name or an abbreviation.
_STATED_ZONE = re.compile(r"\s*\(?([A-Z][A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)+|[A-Z]{2,5})\b")
_MODEL_SCOPE = re.compile(r"\bseven_day_([a-z0-9][a-z0-9_-]*)\b", re.IGNORECASE)


def is_provider_usage_or_quota_limit(value: object) -> bool:
    """Return true when provider text proves account or plan exhaustion."""
    low = str(value).lower()
    return (
        "usage limit" in low
        or "usage_limit" in low
        or ("usage" in low and ("rate limit" in low or "rate_limit" in low))
        or "weekly limit" in low
        or "daily limit" in low
        or "monthly limit" in low
        or "plan limit" in low
        or "quota" in low
        # Claude Code's throttle wording. On 2026-08-19, 35 of 36 recovered
        # limits said "You've hit your session limit" or "weekly limit" (#1177).
        or "session limit" in low
        # Codex: "workspace is out of credits".
        or "out of credits" in low
        or "credit balance" in low
        or "insufficient credit" in low
        or "billing" in low
        or "subscription limit" in low
        or "usage cap" in low
        or (
            "reached your limit" in low
            and any(
                marker in low
                for marker in (
                    "usage",
                    "daily",
                    "weekly",
                    "monthly",
                    "week",
                    "today",
                    "month",
                    "plan",
                    "quota",
                    "subscription",
                )
            )
        )
    )


def model_scope_from_limit(value: object) -> str | None:
    """Return a vendor-named model tier from a limit signal, never infer one."""
    match = _MODEL_SCOPE.search(str(value))
    return match.group(1).lower() if match is not None else None


def _future_aware_timestamp(value: object, *, now: datetime) -> datetime | None:
    """Accept only a timezone-aware ISO timestamp that is still in the future."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed if parsed > now else None


def _local_occurrence(
    date_value, hour: int, minute: int, zone: tzinfo,
) -> datetime | None:
    """Resolve one local clock time only when its offset is unambiguous and real."""
    naive = datetime.combine(date_value, datetime.min.time()).replace(
        hour=hour, minute=minute,
    )
    candidates = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive:
            candidates.append(candidate)
    offsets = {candidate.utcoffset() for candidate in candidates}
    if len(offsets) != 1 or not candidates:
        return None
    return candidates[0]


def _host_zone() -> ZoneInfo | None:
    """Resolve the host's IANA zone; a fixed current offset cannot prove DST."""
    name = os.environ.get("TZ")
    if name is None:
        path = Path("/etc/localtime").resolve()
        parts = path.parts
        if "zoneinfo" not in parts:
            return None
        name = "/".join(parts[parts.index("zoneinfo") + 1:])
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def provider_reset_deadline(
    value: object,
    *,
    reset_at: object | None = None,
    now: datetime | None = None,
    zone: tzinfo | None = None,
) -> datetime | None:
    """Return a strict future provider reset deadline, never a best-effort guess.

    A reset belongs to a terminal provider-limit error, not an ordinary assistant
    message that happens to discuss limits. Harness timestamps must be aware;
    provider prose is limited to an anchored local ``resets H:MMam/pm`` form,
    read in the zone it states or else the host's. A stated zone that cannot be
    resolved or disagrees with the host's abbreviation is unknown. Ambiguous and
    nonexistent DST wall times deliberately fall back to generic liveness
    rather than delaying work on an invented deadline.
    """
    if not is_provider_usage_or_quota_limit(value):
        return None
    observed = now or datetime.now(timezone.utc)
    if reset_at is not None:
        return _future_aware_timestamp(reset_at, now=observed)
    match = _LOCAL_RESET.search(str(value))
    if match is None:
        return None
    local_zone = zone or _host_zone()
    abbreviation = None
    stated = _STATED_ZONE.match(str(value), match.end())
    if stated is not None and "/" in stated.group(1):
        try:
            local_zone = ZoneInfo(stated.group(1))
        except (ValueError, ZoneInfoNotFoundError):
            return None
    elif stated is not None and stated.group(1) in ("UTC", "GMT"):
        local_zone = timezone.utc
    elif stated is not None:
        abbreviation = stated.group(1)
    if local_zone is None:
        return None
    local_now = observed.astimezone(local_zone)
    hour_value = int(match["hour"]) % 12 + (12 if match["meridiem"].lower() == "p" else 0)
    minute = int(match["minute"] or 0)
    day = local_now.date()
    if match["month"] is not None:
        month = _MONTHS.get(match["month"][:3].lower())
        if month is None:
            return None
        year = int(match["year"]) if match["year"] else local_now.year
        try:
            day = day.replace(year=year, month=month, day=int(match["day"]))
        except ValueError:
            return None
        if match["year"] is None and month < local_now.month - 6:
            day = day.replace(year=year + 1)
    candidate = _local_occurrence(day, hour_value, minute, local_zone)
    if candidate is None:
        return None
    if candidate <= local_now and match["month"] is None:
        candidate = _local_occurrence(
            day + timedelta(days=1), hour_value, minute, local_zone,
        )
    if candidate is None or candidate <= local_now:
        return None
    return None if abbreviation not in (None, candidate.tzname()) else candidate
