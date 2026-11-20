"""Updates their agent replaced after the check notice, while unseen, and how long after it each came."""

from __future__ import annotations

import pytest

USER = {"id": "steve"}
SERIES = ("updates_replaced_after_notice_pct", "update_replace_delay_median_seconds", "update_replace_delay_p90_seconds")


def _seed_thread(db, thread_id: str, user_id: str = "steve") -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
        (thread_id, user_id, thread_id, "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
    )


def _seed_updates(db, thread_id: str, day: str, *, kept: int = 0, delays: tuple[int, ...] = (),
                  before_notice: int = 0, no_notice: int = 0, kind: str = "post_update") -> None:
    """Updates posted at 10:00 with the notice shown a minute later.

    ``kept`` are never replaced; ``delays`` are replaced that many minutes after the notice;
    ``before_notice`` were replaced before it; ``no_notice`` were replaced but carry no notice time.
    """
    plan = ([(None, True)] * kept + [(minutes, True) for minutes in delays]
            + [(-1, True)] * before_notice + [(5, False)] * no_notice)
    rows = []
    for index, (minutes, noticed) in enumerate(plan):
        posted, notice = f"{day}T10:00:{index:02d}", f"{day}T10:01:{index:02d}"
        replaced = None
        if minutes is not None:
            total = 1 + minutes
            replaced = f"{day}T{10 + total // 60:02d}:{total % 60:02d}:{index:02d}"
        rows.append((f"{thread_id}-{day}-{kind}-{index}", thread_id, posted, kind,
                     f"{thread_id}-next-{index}" if replaced else None, replaced,
                     "corrects" if replaced else None, notice if noticed else None))
    db.executemany(
        "INSERT INTO message_update_state (message_id,thread_id,posted_at,update_kind,presence_at_post,"
        "replaced_by,replaced_at,replace_reason,notice_at) VALUES (?,?,?,?,'away',?,?,?,?)",
        rows,
    )


async def _rows(grain: str = "weeks") -> dict[str, dict[str, dict]]:
    from api.measurement import measurement_series

    body = await measurement_series(",".join(SERIES), project="all", agent="none", grain=grain,
                                    timezone_name="UTC", user=USER)
    rows: dict[str, dict[str, dict]] = {name: {} for name in SERIES}
    for row in body["rows"]:
        if row["series"] in rows:
            rows[row["series"]][row["period"]] = row
    return rows


@pytest.mark.asyncio
async def test_every_replacement_after_the_notice_counts_with_its_delay(isolated_db, measurement_clock):
    """No cutoff: a replacement an hour after the notice counts like one a minute after it.

    The share is over updates shown the notice; one replaced before its notice, or posted before
    notice times were recorded, is not a reply to it. Reminders are not updates an agent wrote, and
    another person's chats are outside the scope. The delays are the median and 90th percentile,
    nearest rank, served with the count they stand on.
    """
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_thread(db, "someone-else", user_id="other")
        delays = (1, 2, 3, 4, 5, 6, 7, 8, 9, 60)
        _seed_updates(db, "root", "2026-09-23", kept=8, delays=delays, before_notice=2, no_notice=3)
        _seed_updates(db, "root", "2026-09-23", kept=5, kind="reminder_fallback")
        _seed_updates(db, "someone-else", "2026-09-23", delays=(1,) * 20)
        db.commit()
    finally:
        db.close()

    rows = await _rows()
    share = rows["updates_replaced_after_notice_pct"]["2026-09-21"]
    assert (share["n"], share["value"]) == (20, pytest.approx(10 / 20))
    median = rows["update_replace_delay_median_seconds"]["2026-09-21"]
    p90 = rows["update_replace_delay_p90_seconds"]["2026-09-21"]
    assert (median["n"], median["value"]) == (10, 5 * 60)
    assert (p90["n"], p90["value"]) == (10, 9 * 60)


@pytest.mark.asyncio
async def test_windows_pool_the_spread_and_unmeasured_periods_stay_empty(isolated_db, measurement_clock):
    """The comparison windows read the pooled weeks' own spread, not an average of medians, and a
    week before the first notice is not measured."""
    from infra.database import get_db

    db = get_db()
    try:
        _seed_thread(db, "root")
        _seed_updates(db, "root", "2026-09-16", kept=10, delays=(1, 1, 1))
        _seed_updates(db, "root", "2026-09-23", kept=10, delays=(30,) * 12)
        db.commit()
    finally:
        db.close()

    rows = await _rows()
    early = rows["updates_replaced_after_notice_pct"]
    assert all(early[period]["value"] is None and early[period]["n"] == 0 for period in early if period < "2026-09-14")
    assert rows["update_replace_delay_median_seconds"]["2026-09-14"]["value"] == 60
    assert rows["update_replace_delay_median_seconds"]["2026-09-21"]["value"] == 30 * 60
    assert early["2026-09-14"]["n"] == 13 and early["2026-09-14"]["value"] is None
    assert early["2026-09-14"]["raw_value"] == pytest.approx(3 / 13)

    from api.measurement_updates import update_value
    from api.measurement_windows import _summed

    pooled = _summed([
        {"n": 13, "replaced_after_notice": 3, "delays": frozenset({("a", 60), ("b", 60), ("c", 60)})},
        {"n": 22, "replaced_after_notice": 12, "delays": frozenset((f"d{i}", 1800) for i in range(12))},
    ])
    assert update_value("update_replace_delay_median_seconds", pooled) == (1800, 15)
    assert update_value("updates_replaced_after_notice_pct", pooled) == (pytest.approx(15 / 35), 35)


def test_nearest_rank_matches_the_limits_reading() -> None:
    """One quantile rule for every spread on the page: a value that occurred, never interpolated."""
    from api.measurement_updates import nearest_rank

    assert nearest_rank([], 1, 2) is None
    assert [nearest_rank(list(range(1, n + 1)), 1, 2) for n in (1, 2, 3, 4, 5)] == [1, 1, 2, 2, 3]
    assert [nearest_rank(list(range(1, n + 1)), 9, 10) for n in (1, 9, 10, 11, 20)] == [1, 9, 9, 10, 18]


def test_migration_adds_the_notice_time_once(isolated_db) -> None:
    """Schema 201 adds ``notice_at``; re-running the step, or a database without the table, is a no-op."""
    import sqlite3

    from infra.migration_200_to_201_update_notice_at import migrate_200_to_201

    conn = sqlite3.connect(":memory:")
    migrate_200_to_201(conn)
    conn.execute("CREATE TABLE message_update_state (message_id TEXT PRIMARY KEY, posted_at TEXT)")
    migrate_200_to_201(conn)
    migrate_200_to_201(conn)
    assert "notice_at" in {row[1] for row in conn.execute("PRAGMA table_info(message_update_state)")}
