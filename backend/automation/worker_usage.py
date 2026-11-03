"""The model a settled worker ran as and what its calls cost, for its ``worker.*`` event.

Both come from the reads Measurement's Cost and Models use (``grouped_usage_sql``,
``IdentityResolver``, ``classify_usage_rows``). A value CORSIA does not know is left out:
a model that is not one identity, or a cost with a call Measurement cannot price or a
local model that has no API price, is absent, never a partial sum or a guess.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from core.cost_identity import IdentityResolver
from core.measurement_pricing import classify_usage_rows, endpoint_listings, grouped_usage_sql


def facts(db: sqlite3.Connection, worker: str) -> dict[str, Any]:
    """``model`` and ``cost_usd`` of the worker thread, each only when known."""
    rows = [dict(row) for row in db.execute(
        grouped_usage_sql({"thread": "u.thread_id"}, "llm_usage u WHERE u.thread_id=?"), (worker,))]
    resolve = IdentityResolver(db)
    for row in rows:
        row["model"] = resolve(row).key
    models = {row["model"] for row in rows}
    if not rows:  # no call recorded: the model it was launched with
        launched = db.execute("SELECT model FROM threads WHERE id=?", (worker,)).fetchone()
        if launched is not None and launched["model"]:
            models = {resolve({"model": launched["model"]}).key}
    known: dict[str, Any] = {"model": models.pop()} if len(models) == 1 else {}
    classified = [row for row in classify_usage_rows(rows, endpoint_listings(db))
                  if row["outcome"] != "not_api_priced"]
    if classified and all(row["outcome"] == "priced" for row in classified):
        known["cost_usd"] = sum(row["dollars"] for row in classified)
    return known
