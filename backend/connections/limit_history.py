"""Append-only history of an account's limit-window readings.

``vendor_accounts.usage_json`` keeps only the latest reading of each window.
``harness_usage.record_usage`` calls ``append_changed`` in the transaction that
stores the merged state, so a window's history and its latest reading commit
together. Nothing here reads the table: a reading is compared with the window
as it was stored before this write, which the writer already holds.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _reading(window: object) -> tuple[Any, Any]:
    if not isinstance(window, Mapping):
        return None, None
    # The reset that belongs to the reading: a frame carrying only a new reset
    # leaves the old percentage in place, and that pair was never observed.
    return window.get("used_percent"), (
        window.get("used_percent_resets_at") or window.get("resets_at")
    )


def append_changed(
    db, account_id: str, before: Mapping[str, Any] | None, after: Mapping[str, Any],
) -> int:
    """Append one row for each stored window whose percentage or reset changed.

    ``before`` and ``after`` are the account's windows by key around one write.
    A window with no percentage after the merge has no reading to record.
    Returns the rows added.
    """
    added = 0
    for key, window in after.items():
        percent, resets_at = _reading(window)
        observed_at = window.get("used_percent_observed_at") or window.get("observed_at")
        kind = window.get("kind")
        if percent is None or not observed_at or not kind:
            continue
        if _reading((before or {}).get(key)) == (percent, resets_at):
            continue
        db.execute(
            "INSERT INTO limit_observations "
            "(account_id,window_key,window_kind,used_percent,resets_at,source,observed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                account_id, key, str(kind), percent, resets_at,
                str(window.get("source") or "unknown"), str(observed_at),
            ),
        )
        added += 1
    return added
