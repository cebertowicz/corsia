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

from loguru import logger

from core.usage_fields import DEV_RELEASE, RELEASE_PATTERN, USAGE_EVENT
from infra.database import write_transaction
from infra.outcome_events import record_outcome_event

_RELEASE = re.compile(RELEASE_PATTERN)
_OS_BY_SYSTEM = {"Darwin": "darwin", "Windows": "windows", "Linux": "linux"}
_CHIP_BY_MACHINE = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}


def iso_week(now: datetime) -> str:
    """The ISO week of a UTC time, such as ``2026-W44``."""
    year, week, _ = now.astimezone(timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


def release_tag(version: str | None) -> str:
    """The release a build reports: a plain ``major.minor.patch`` tag, else ``dev``."""
    text = (version or "").strip()
    return text if _RELEASE.fullmatch(text) else DEV_RELEASE


def platform_fields() -> dict[str, str]:
    """This machine's ``os.type`` and ``host.arch``; unlisted values are left out."""
    found = {"os.type": _OS_BY_SYSTEM.get(platform.system()), "host.arch": _CHIP_BY_MACHINE.get(platform.machine().lower())}
    return {key: value for key, value in found.items() if value}


def record_install_week(
    db: sqlite3.Connection, *, now: datetime | None = None, version: str | None = None,
    fields: dict[str, str] | None = None,
) -> bool:
    """Record, once per ISO week, that this install was active; True when this call wrote it.

    Local whatever the sharing choice. The first record of a week wins, so a mid-week
    update keeps the week's first release. Every value comes from ``iso_week``,
    ``release_tag`` or ``platform_fields``, and the outcome-event allowlist enforces the
    value rules on write. A recorded week costs one plain read and never takes the write
    lock; the re-check inside the write transaction closes the race between two writers, so
    neither reaches the event store's conflict branch or counts as a duplicate delivery.
    """
    moment = now or datetime.now(timezone.utc)
    week = iso_week(moment)
    dedupe_key = f"{USAGE_EVENT}:{week}"
    recorded = "SELECT 1 FROM outcome_events WHERE dedupe_key=?"
    if db.execute(recorded, (dedupe_key,)).fetchone():
        return False
    attributes = {"corsia.usage.week": week, "service.version": release_tag(version), **(platform_fields() if fields is None else fields)}
    with write_transaction(db, scope="record_install_week") as connection:
        if connection.execute(recorded, (dedupe_key,)).fetchone():
            return False
        result = record_outcome_event(
            connection, dedupe_key=dedupe_key, event_name=USAGE_EVENT, source="corsia",
            provenance="instrumented", ts=moment.isoformat(), attribution="none", attributes=attributes,
        )
    return result.inserted


def record_current_install_week(now: datetime | None = None) -> None:
    """Record this week, at startup and from the periodic sweep; a recorded week is a no-op.

    A failure is logged and never stops the app; the week is retried on the next call.
    """
    try:
        from infra.build_info import app_version
        from infra.database import get_db

        record_install_week(get_db(), now=now, version=app_version())
    except Exception as exc:  # noqa: BLE001  # local usage recording is non-load-bearing
        logger.warning("usage summary: could not record this install-week: {}", exc)
