"""Persist and apply a person's ordering for one activity scope."""

from __future__ import annotations

import json

# The approved Activity design shows each reason on one line under its item. At the list's
# widest (455 px, a window 1,264 px or wider), after the 'Why it's here: ' prefix, one line of
# 11.5 px system font holds 63-73 characters of ordinary text (measured with SF Pro; Fortissimo's
# real reasons fit up to 66). 64 keeps a reason to one line there; narrower lists wrap it.
REASON_MAX_CHARS = 64


def validate_order(items: list[dict], current_keys: set[str]) -> None:
    """Refuse stale, duplicate or overlong items before replacing an order."""
    seen: set[str] = set()
    for item in items:
        key = item.get("key")
        if not isinstance(key, str) or key not in current_keys:
            raise ValueError(f"Unknown key {key!r}; re-read the queue and try again.")
        if key in seen:
            raise ValueError(f"Key {key!r} appears twice; list each item once.")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"Give {key!r} a reason written for the person.")
        if len(reason) > REASON_MAX_CHARS:
            raise ValueError(f"Reason for {key!r} exceeds {REASON_MAX_CHARS} characters; shorten it.")
        if "\n" in reason or "\r" in reason:
            raise ValueError(f"Reason for {key!r} must be one line.")
        seen.add(key)


def replace_ranks(db, user_id: str, scope: str, ranker: str, items: list[dict], now: str,
                  current_keys: set[str]) -> None:
    """Replace a scope inside the caller's transaction after the caller validates the list.

    A key missing from the queue the ranker read (``current_keys``) loses its rank here, so it
    also leaves the last wake's seen keys: if it comes back unranked, it is new again and wakes
    the ranker (sources/activity_ranking.py).
    """
    db.execute("DELETE FROM activity_ranks WHERE user_id=? AND scope=?", (user_id, scope))
    row = db.execute("SELECT seen_keys FROM activity_rank_nudges WHERE user_id=? AND scope=?",
                     (user_id, scope)).fetchone()
    if row:
        kept = sorted(set(json.loads(row[0])) & current_keys)
        db.execute("UPDATE activity_rank_nudges SET seen_keys=? WHERE user_id=? AND scope=?",
                   (json.dumps(kept), user_id, scope))
    db.executemany(
        "INSERT INTO activity_ranks (user_id, scope, item_key, position, reason, ranked_by, ranked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(user_id, scope, item["key"], position, item["reason"], ranker, now)
         for position, item in enumerate(items, start=1)],
    )


def apply_ranks(db, user_id: str, scope: str, items: list[dict]) -> list[dict]:
    """Put ranked current items first, preserving canonical order for unranked items."""
    ranks = {
        row["item_key"]: {"position": row["position"], "reason": row["reason"],
                          "ranked_by": row["ranked_by"], "ranked_at": row["ranked_at"]}
        for row in db.execute(
            "SELECT item_key, position, reason, ranked_by, ranked_at FROM activity_ranks "
            "WHERE user_id=? AND scope=?", (user_id, scope),
        )
    }
    annotated = [{**item, "rank": ranks.get(item["key"])} for item in items]
    return sorted(annotated, key=lambda item: (
        item["rank"] is None, item["rank"]["position"] if item["rank"] is not None else 0,
    ))
