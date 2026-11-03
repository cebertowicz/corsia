"""The model a settled worker ran as, for its ``worker.*`` event.

It comes from the identity read Measurement's Models uses (``grouped_usage_sql``,
``IdentityResolver``). A model that is not one resolved identity is left out, never
guessed. What the worker's calls cost is not a fact here: that rule has one owner,
``plans/usage_cost.py``, and reaches this event once ``plans.public`` exports it.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from core.cost_identity import IdentityResolver
from core.measurement_pricing import grouped_usage_sql


def facts(db: sqlite3.Connection, worker: str) -> dict[str, Any]:
    """``model`` of the worker thread, only when it is one resolved identity."""
    rows = [dict(row) for row in db.execute(
        grouped_usage_sql({"thread": "u.thread_id"}, "llm_usage u WHERE u.thread_id=?"), (worker,))]
    resolve = IdentityResolver(db)
    identities = [resolve(row) for row in rows]
    if not rows:  # no call recorded: the model it was launched with
        launched = db.execute("SELECT model FROM threads WHERE id=?", (worker,)).fetchone()
        if launched is not None and launched["model"]:
            identities = [resolve({"model": launched["model"]})]
    # An alias the catalog can't resolve is not an identity, so it never stands in for one.
    models = {identity.key for identity in identities}
    one = len(models) == 1 and all(identity.resolved for identity in identities)
    return {"model": models.pop()} if one else {}
