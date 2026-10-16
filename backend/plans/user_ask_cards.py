"""Read projection for user-owned artifact review asks."""

from __future__ import annotations

import json

from plans.node_states import open_user_block_sql


def ask_holds(db, hold_ids) -> dict[str, dict]:
    """The user asks among ``hold_ids``, by hold id, read by primary key.

    Each is ``{artifact_id, open, raised_in, answer}``: the artifact asked about,
    whether the ask is still open, where the agent raised it (``turn_id``,
    ``tool_invocation_id``, ``provider_call_id``; empty for a hold placed before
    they were recorded), and what its ending recorded (``verdict``,
    ``feedback_counts``, ``ended``; empty when it recorded none). A hold that is
    not a user ask, or does not exist, is absent.
    """
    ids = list(dict.fromkeys(hold_ids))
    if not ids:
        return {}
    rows = _ask_rows(db, ids)
    endings = _endings(db, rows.values())
    placed = _placed_raised_in(db, set(rows), {row["node_id"] for row in rows.values()})
    return {
        hold_id: {
            "artifact_id": row["owner_id"],
            "open": bool(row["is_open"]),
            "raised_in": placed.get(hold_id, {}),
            "answer": {
                key: value for key, value in endings.get(row["clearance_event_id"], {}).items()
                if key in ("ended", "verdict", "feedback_counts")
            },
        }
        for hold_id, row in rows.items()
    }


def _ask_rows(db, hold_ids) -> dict[str, dict]:
    """The user asks among ``hold_ids``, by hold id, read by primary key."""
    marks = ",".join("?" * len(hold_ids))
    return {
        row["id"]: dict(row)
        for row in db.execute(
            f"""SELECT h.id, h.node_id, h.owner_id, h.clearance_event_id,
                       {open_user_block_sql('h')} AS is_open
                  FROM plan_node_holds h
                 WHERE h.id IN ({marks}) AND h.kind='blocked' AND h.owner_kind='user'
                   AND h.owner_id IS NOT NULL""",
            hold_ids,
        )
    }


def _endings(db, rows) -> dict[str, dict]:
    """Each cleared hold's ending detail, by its clearance event id."""
    event_ids = [row["clearance_event_id"] for row in rows if row["clearance_event_id"]]
    if not event_ids:
        return {}
    marks = ",".join("?" * len(event_ids))
    endings = {}
    for row in db.execute(
        f"SELECT id,payload_json,provenance FROM plan_node_events WHERE id IN ({marks})", event_ids,
    ):
        payload = _decoded(row["payload_json"])
        detail = payload.get("detail") if payload is not None else None
        if isinstance(detail, dict):
            detail = dict(detail)
            # The original v120 wrote withdrawn for a task-ending migration.
            # Its unique provenance positively identifies that path; legacy
            # withdraw_node_asks rows lack causal evidence and remain withdrawn.
            if (detail.get("ended") == "withdrawn"
                    and row["provenance"] == "migration.119_to_120"):
                detail["ended"] = "closed"
            endings[row["id"]] = detail
    return endings


def _placed_raised_in(db, hold_ids, node_ids) -> dict[str, dict]:
    """Where each hold was raised, from its ``hold_placed`` event."""
    if not node_ids:
        return {}
    marks = ",".join("?" * len(node_ids))
    found = {}
    for row in db.execute(
        f"""SELECT payload_json FROM plan_node_events
             WHERE node_id IN ({marks}) AND event='hold_placed'""",
        sorted(node_ids),
    ):
        payload = _decoded(row["payload_json"])
        if payload is None or payload.get("hold_id") not in hold_ids:
            continue
        raised_in = payload.get("raised_in")
        found[payload["hold_id"]] = {
            key: value for key, value in raised_in.items() if isinstance(value, str) and value
        } if isinstance(raised_in, dict) else {}
    return found


def _decoded(raw) -> dict | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None
