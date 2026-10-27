"""Local recording of this install's weekly usage.

Recording is always on and local: it never reads a sharing setting and makes no network
call or model call. The record holds only the closed field list in ``core.usage_fields``;
nothing leaves the machine from here.
"""
from __future__ import annotations

import platform
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from core.usage_fields import (
    CHIPS, DEV_RELEASE, FIELDS, OPTIONAL_FIELDS, OS_FAMILIES, RELEASE_PATTERN, REQUIRED_FIELDS, USAGE_EVENT,
    WEEK_PATTERN,
)
from infra.database import write_transaction
from infra.outcome_events import record_outcome_event

_WEEK = re.compile(WEEK_PATTERN)
_RELEASE = re.compile(RELEASE_PATTERN)
_OS_BY_SYSTEM = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}
_CHIP_BY_MACHINE = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}
_ENUMS = {"os": OS_FAMILIES, "chip": CHIPS}


class SummaryError(ValueError):
    """A record holds a field outside the closed list, or a value outside its set."""


def iso_week(now: datetime) -> str:
    """The ISO week of a UTC time, such as ``2026-W44``."""
    year, week, _ = now.astimezone(timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


def release_tag(version: str | None) -> str:
    """The release a build reports: a plain ``major.minor.patch`` tag, else ``dev``."""
    text = (version or "").strip()
    return text if _RELEASE.fullmatch(text) else DEV_RELEASE


def platform_fields() -> dict[str, str]:
    """This machine's OS family and chip family; one that is on neither list is left out."""
    found = {"os": _OS_BY_SYSTEM.get(platform.system()), "chip": _CHIP_BY_MACHINE.get(platform.machine().lower())}
    return {key: value for key, value in found.items() if value}


def validate_summary(summary: Any) -> dict[str, str]:
    """Return the record when it is exactly the closed schema, else raise ``SummaryError``.

    Any field outside the list fails the whole record; a value outside its set is never
    rewritten into a plausible one.
    """
    if not isinstance(summary, dict):
        raise SummaryError("a summary is a JSON object")
    extra = sorted(str(key) for key in summary if key not in FIELDS)
    if extra:
        raise SummaryError(f"fields outside the closed list: {', '.join(extra)}")
    missing = [key for key in REQUIRED_FIELDS if key not in summary]
    if missing:
        raise SummaryError(f"missing fields: {', '.join(missing)}")
    week, release = summary["week"], summary["release"]
    if not (isinstance(week, str) and _WEEK.fullmatch(week)):
        raise SummaryError("week is not an ISO week such as 2026-W44")
    if not (isinstance(release, str) and _RELEASE.fullmatch(release)):
        raise SummaryError("release is not a release tag or dev")
    for key in OPTIONAL_FIELDS:
        if key in summary and summary[key] not in _ENUMS[key]:
            raise SummaryError(f"{key} is not one of {', '.join(_ENUMS[key])}")
    return {key: summary[key] for key in FIELDS if key in summary}


def record_install_week(
    db: sqlite3.Connection, *, now: datetime | None = None, version: str | None = None,
    fields: dict[str, str] | None = None,
) -> bool:
    """Record, once per ISO week, that this install was active; True when this call wrote it.

    Local whatever the sharing choice. The first record of a week wins, so a mid-week
    update keeps the week's first release. The existing-week check and the insert share one
    write transaction, so a restart or a second writer in a recorded week never reaches the
    event store's conflict branch and never counts as a duplicate delivery.
    """
    moment = now or datetime.now(timezone.utc)
    week = iso_week(moment)
    summary = validate_summary({
        "week": week, "release": release_tag(version), **(platform_fields() if fields is None else fields),
    })
    dedupe_key = f"{USAGE_EVENT}:{week}"
    with write_transaction(db, scope="record_install_week") as connection:
        if connection.execute("SELECT 1 FROM outcome_events WHERE dedupe_key=?", (dedupe_key,)).fetchone():
            return False
        result = record_outcome_event(
            connection, dedupe_key=dedupe_key, event_name=USAGE_EVENT, source="corsia",
            provenance="instrumented", ts=moment.isoformat(), attribution="none", attributes=summary,
        )
    return result.inserted


def record_current_install_week(now: datetime | None = None) -> None:
    """Record this week, at startup and from the periodic sweep; a recorded week is a no-op.

    A failure is logged and never stops the app; the week is retried on the next call.
    """
    from loguru import logger

    from infra.build_info import app_version
    from infra.database import get_db

    try:
        record_install_week(get_db(), now=now, version=app_version())
    except Exception as exc:  # noqa: BLE001  # local usage recording is non-load-bearing
        logger.warning("usage summary: could not record this install-week: {}", exc)
