"""One node's durable history, and the exact read of a node that left the plan.

Split from ``plans.status`` because the retired read shares only the event
history with the live one: a retired node has no live execution, review, wait
or position to project, and ``status`` builds its detail from the live
projection that archived rows never enter.

That is why the exact-node read used to refuse every archived node. Archive
promises the row, its events and its links survive; the read could not show
them. Closing made that gap load-bearing while it archived in the same write;
a closed node now stays on its plan (#1887), and every archived node is read
here.
"""

from __future__ import annotations

import json
from typing import Any

from plans import operator_node_state

#: Newest events an exact read returns. Detail is a deliberate additional read,
#: but a long-lived node's history is still bounded.
DETAIL_EVENTS = 20


def events(db, node_id: str) -> list[dict[str, Any]]:
    """Return one node's newest events, newest first, with decoded payloads."""
    rows = db.execute(
        """SELECT id,event,prior_state,new_state,actor_id,provenance,payload_json,created_at
           FROM plan_node_events WHERE node_id=?
           ORDER BY created_at DESC,rowid DESC LIMIT ?""",
        (node_id, DETAIL_EVENTS),
    ).fetchall()
    return [
        {
            "id": row["id"], "event": row["event"], "prior_state": row["prior_state"],
            "new_state": row["new_state"], "actor_id": row["actor_id"],
            "provenance": row["provenance"], "created_at": row["created_at"],
            "payload": json.loads(row["payload_json"] or "null"),
        }
        for row in rows
    ]


def retired_detail(db, durable, *, clock: str) -> dict[str, Any]:
    """Return an archived node exactly: its row, its outcome, and its history.

    Any archived node, not only a closed one: every retirement is served the
    same way, so "archived" is never a special case of itself. The display is
    derived from the durable state alone, because nothing live remains.
    """
    return {
        **dict(durable),
        "display": operator_node_state.retired_display(str(durable["state"])),
        "events": events(db, str(durable["id"])),
        "as_of": clock,
    }
