"""Priced model cost of whole threads, for the KPI ``cost_of`` operand."""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from infra.turn_usage_remainder import short_threads, thread_measurement_remainder
from plans.public import usage_dollars


def thread_costs(db: sqlite3.Connection, user_id: str, thread_ids: Iterable[str]) -> dict[str, float | None]:
    """Each thread's whole-life priced cost in USD, or None when any of its usage can't be priced.

    One ``usage_dollars`` read for every id the user owns. A thread with no usage rows, or one the user
    doesn't own, is absent: its cost is unknown. A thread that ran only on local models costs 0. A thread
    the ledger still owes a measurement, or that ended a Turn with none (``short_threads``), is unknown:
    its rows are not all it spent. What is owed is read before the rows, so a call committing between the
    two reads is counted unknown rather than missed.
    """
    ids = sorted(set(thread_ids))
    owned = [row[0] for row in db.execute(
        "SELECT id FROM threads WHERE user_id=? AND id IN (SELECT value FROM json_each(?))",
        (user_id, json.dumps(ids)))] if ids else []
    owed, settled = thread_measurement_remainder(db, owned)
    priced = usage_dollars(db, {"thread_id": owned}, include_turns=True)["thread_id"]
    short = short_threads(owed, settled, priced)
    return {thread: None if thread in short or amount["unknown_rows"] else (amount["known"] or 0.0)
            for thread, amount in priced.items() if amount["records"]}
