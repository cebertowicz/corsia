"""Schema 75 -> 76: legacy harness-usage verdicts become the scoped verdict map.

#2477 (amendment 16, E): the running code reads only ``usage.vendor_verdicts``,
keyed by scope. Two legacy shapes can still sit in a harness connection's
config: the single ``usage.vendor_verdict``, and the older top-level
``usage_limit``. Both were account-wide, so each becomes a candidate for the
account scope. Every row carrying either key is rewritten once, here, and both
keys are dropped; nothing in the running code reads them.

A legacy candidate replaces the verdict already kept for the account scope only
when it has an ``observed_at`` newer than that verdict's. One without a time
never wins over an existing verdict, and nothing invents a time for it.

This step is frozen: it imports none of the running usage code, and it writes
the stored shape as it is at schema 76. Runs inside the runner's transaction;
it never commits or rolls back.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

_PROVIDERS = ("claude-code", "codex")
_ACCOUNT_SCOPE = "null"


def _time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _from_usage_limit(legacy: dict[str, Any]) -> dict[str, Any]:
    verdict = {
        "state": "limited", "reason": legacy.get("reason"), "resets_at": legacy.get("resets_at"),
        "source": legacy.get("source", "migration"), "scope_model": None,
    }
    if legacy.get("observed_at"):
        verdict["observed_at"] = legacy["observed_at"]
    return verdict


def _replaces(candidate: dict[str, Any], kept: dict[str, Any] | None) -> bool:
    if kept is None:
        return True
    candidate_time = _time(candidate.get("observed_at"))
    kept_time = _time(kept.get("observed_at"))
    return candidate_time is not None and (kept_time is None or candidate_time > kept_time)


def _migrate_config(config: dict[str, Any]) -> bool:
    legacy_limit = config.pop("usage_limit", None)
    usage = config.get("usage")
    usage = dict(usage) if isinstance(usage, dict) else {"version": 1, "windows": {}, "facts": {}}
    legacy_verdict = usage.pop("vendor_verdict", None)
    if legacy_limit is None and legacy_verdict is None:
        return False
    stored = usage.get("vendor_verdicts")
    verdicts = dict(stored) if isinstance(stored, dict) else {}
    kept = verdicts.get(_ACCOUNT_SCOPE)
    kept = kept if isinstance(kept, dict) else None
    if isinstance(legacy_verdict, dict):
        candidate = {**legacy_verdict, "scope_model": None}
        if _replaces(candidate, kept):
            kept = candidate
    if isinstance(legacy_limit, dict):
        candidate = _from_usage_limit(legacy_limit)
        if _replaces(candidate, kept):
            kept = candidate
            if legacy_limit.get("check_error"):
                usage["last_check"] = {
                    "outcome": "unknown", "at": legacy_limit.get("checked_at") or legacy_limit.get("observed_at"),
                    "error": str(legacy_limit["check_error"])[:300],
                }
    if kept is not None:
        verdicts[_ACCOUNT_SCOPE] = kept
        usage["vendor_verdicts"] = verdicts
    config["usage"] = usage
    return True


def migrate_75_to_76(conn: sqlite3.Connection) -> None:
    """Rewrite every harness connection that still carries a legacy verdict key."""
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "connections" not in tables:
        return
    rows = conn.execute(
        "SELECT provider, config FROM connections WHERE provider IN (?, ?)", _PROVIDERS,
    ).fetchall()
    for provider, raw in rows:
        config = json.loads(raw) if raw else {}
        if not isinstance(config, dict) or not _migrate_config(config):
            continue
        conn.execute(
            "UPDATE connections SET config = ? WHERE provider = ?",
            (json.dumps(config, sort_keys=True), provider),
        )
