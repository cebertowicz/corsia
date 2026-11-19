"""The share of updates their agent replaced soon after posting, while the person had not seen them."""

from __future__ import annotations

import pytest

USER = {"id": "steve"}


def _seed_thread(db, thread_id: str, user_id: str = "steve") -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
        (thread_id, user_id, thread_id, "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
    )


def _seed_updates(db, thread_id: str, day: str, *, kept: int, soon: int = 0, late: int = 0,
                  kind: str = "post_update") -> None:
    """``soon`` replaced 2 minutes after posting, ``late`` after an hour, ``kept`` never replaced."""
    rows = []
    for index in range(kept + soon + late):
        posted = f"{day}T10:{index:02d}:00Z"
        replaced = (None if index < kept else
                    f"{day}T10:{index + 2:02d}:00Z" if index < kept + soon else
                    f"{day}T11:{index:02d}:00Z")
        rows.append((f"{thread_id}-{day}-{kind}-{index}", thread_id, posted, kind,
                     f"{thread_id}-next" if replaced else None, replaced, "corrects" if replaced else None))
    db.executemany(
        "INSERT INTO message_update_state (message_id,thread_id,posted_at,update_kind,presence_at_post,"
        "replaced_by,replaced_at,replace_reason) VALUES (?,?,?,?,'away',?,?,?)",
        rows,
    )


@pytest.mark.asyncio
async def test_only_updates_replaced_within_minutes_of_posting_count(isolated_db, measurement_clock):
    """A week's figure is the updates replaced within minutes over all its updates.

    An update replaced an hour later was overtaken by news, not taken back; reminders are not
    updates an agent wrote; another person's chats are outside the scope; a week with fewer
    updates than the minimum shows no figure.
    """
    from api.measurement import measurement_series
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_thread(db, "someone-else", user_id="other")
        _seed_updates(db, "root", "2026-09-23", kept=15, soon=6, late=4)
        _seed_updates(db, "root", "2026-09-23", kept=5, kind="reminder_fallback")
        _seed_updates(db, "someone-else", "2026-09-23", kept=0, soon=20)
        _seed_updates(db, "root", "2026-09-16", kept=8, soon=2)
        db.commit()
    finally:
        db.close()

    body = await measurement_series("updates_replaced_soon_pct", project="all", agent="none", grain="weeks",
                                    timezone_name="UTC", user=USER)
    rows = {row["period"]: row for row in body["rows"] if row["series"] == "updates_replaced_soon_pct"}
    assert (rows["2026-09-21"]["n"], rows["2026-09-21"]["value"]) == (25, pytest.approx(6 / 25))
    assert rows["2026-09-14"]["n"] == 10
    assert rows["2026-09-14"]["value"] is None and rows["2026-09-14"]["raw_value"] == pytest.approx(0.2)
    assert all(rows[period]["value"] is None for period in rows if period < "2026-09-14"), rows
