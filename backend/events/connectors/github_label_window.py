"""Match the webhook copy and the poller copy of one label or reopen change within a short window.

Both copies reach the legacy GitHub admission (``github_admission``), which records one
outcome row per change under a ``dedupe_key`` that carries the change's time. A label or
reopen webhook takes that time from the item's ``updated_at``, which drifted one second
from the ``issues/events`` row's time on 1 of 3 recorded pairs, so the exact keys of the
two copies can differ. ``seen_in_window`` asks whether a copy within ``WINDOW_SECONDS``
of this one was already recorded; if so the admission settles the delivery as a duplicate
and neither writes its facts nor schedules it. ``issue.opened`` and ``issue.closed`` keep
exact keys (``closed_at`` matched the row time on 3 of 3 pairs).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from events.connectors.github_outcomes import OutcomeDraft

WINDOWED = frozenset({"github.pr.labeled", "github.pr.unlabeled", "github.issue.labeled",
                      "github.issue.unlabeled", "github.issue.reopened"})
WINDOW_SECONDS = 5
STATUS = "duplicate_copy"


def window_keys(draft: OutcomeDraft) -> list[str]:
    """The ``dedupe_key`` a copy of this draft would carry for each time within the window."""
    try:
        at = datetime.fromisoformat(draft.ts.replace("Z", "+00:00"))
    except ValueError:
        return []
    prefix = draft.dedupe_key[:-len(draft.ts)]
    return [prefix + (at + timedelta(seconds=n)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for n in range(-WINDOW_SECONDS, WINDOW_SECONDS + 1) if draft.dedupe_key.endswith(draft.ts)]


def seen_in_window(db: sqlite3.Connection, drafts: list[OutcomeDraft] | None) -> bool:
    """Whether a windowed change in ``drafts`` already has a recorded copy; read-only."""
    for draft in drafts or ():
        keys: list[Any] = window_keys(draft) if draft.event_name in WINDOWED else []
        if keys and db.execute("SELECT 1 FROM outcome_events WHERE dedupe_key IN (%s) LIMIT 1"
                               % ",".join("?" * len(keys)), keys).fetchone():
            return True
    return False
