"""Read projection for user-owned artifact review asks."""

from __future__ import annotations

import json

from plans.node_states import open_user_block_sql


def ask_holds(db, hold_ids) -> dict[str, dict]:
    """The user asks among ``hold_ids``, by hold id, read by primary key in bounded batches.

    Each is ``{artifact_id, open, raised_in, answer}``: the artifact asked about,
    whether the ask is still open, where the agent raised it (``turn_id``,
    ``tool_invocation_id``, ``provider_call_id``; empty for a hold placed before
    they were recorded) and what its ending recorded (``verdict``,
    ``feedback_counts``, ``ended``; empty when it recorded none). A hold an agent
    restated by re-blocking reads as its newest successor on the same task, so an
    earlier card never claims an answer the user did not give. A hold that is not
    a user ask, or does not exist, is absent.
    """
    ids = list(dict.fromkeys(hold_ids))
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = db.execute(
        f"""SELECT h.id, h.node_id, h.owner_id, h.clearance_event_id,
                   {open_user_block_sql('h')} AS is_open
              FROM plan_node_holds h
             WHERE h.id IN ({marks}) AND h.kind='blocked' AND h.owner_kind='user'
               AND h.owner_id IS NOT NULL""",
        ids,
    ).fetchall()
    cleared = [row["clearance_event_id"] for row in rows if row["clearance_event_id"]]
    # holds.clear_write keeps the ending detail under its own ``detail`` key.
    answers = {
        event_id: detail
        for event_id, payload in _event_payloads(db, cleared).items()
        if isinstance(detail := payload.get("detail"), dict)
    }
    restated = {
        (str(row["node_id"]), str(row["owner_id"]))
        for row in rows
        if answers.get(row["clearance_event_id"], {}).get("ended") == "restated"
    }
    successors = _restated_successors(db, restated)
    placed = _placed_raised_in(db, [row["id"] for row in rows], {row["node_id"] for row in rows})
    return {
        row["id"]: _ask_row(row, answers, successors, placed)
        for row in rows
    }


def _ask_row(row, answers: dict, successors: dict, placed: dict) -> dict:
    answer = answers.get(row["clearance_event_id"], {})
    successor = successors.get((str(row["node_id"]), str(row["owner_id"])))
    if answer.get("ended") == "restated" and successor is not None:
        open_ask = successor["open"]
        answer = successor["answer"]
    else:
        open_ask = bool(row["is_open"])
    return {
        "artifact_id": row["owner_id"],
        "open": open_ask,
        "raised_in": placed.get(row["id"], {}),
        "answer": {
            key: value for key, value in answer.items()
            if key in ("verdict", "feedback_counts", "ended")
        },
    }


def _restated_successors(db, restated: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """Read each restated ask's newest same-task successor in one query."""
    if not restated:
        return {}
    pair_marks = ",".join("(?,?)" for _ in restated)
    bindings = [item for pair in sorted(restated) for item in pair]
    rows = db.execute(
        f"""WITH requested(node_id,owner_id) AS (VALUES {pair_marks})
              SELECT h.node_id,h.owner_id,h.clearance_event_id,
                     {open_user_block_sql('h')} AS is_open,e.payload_json
                FROM requested r
                JOIN plan_node_holds h ON h.node_id=r.node_id AND h.owner_id=r.owner_id
                LEFT JOIN plan_node_events e ON e.id=h.clearance_event_id
               WHERE h.kind='blocked' AND h.owner_kind='user'
               ORDER BY h.node_id,h.owner_id,h.created_at DESC,h.id DESC""",
        bindings,
    ).fetchall()
    successors: dict[tuple[str, str], dict] = {}
    for row in rows:
        pair = (str(row["node_id"]), str(row["owner_id"]))
        if pair not in successors:
            payload = _decoded(row["payload_json"])
            detail = payload.get("detail") if payload is not None else None
            successors[pair] = {
                "open": bool(row["is_open"]),
                "answer": detail if isinstance(detail, dict) else {},
            }
    return successors


def _event_payloads(db, event_ids: list[str]) -> dict[str, dict]:
    if not event_ids:
        return {}
    marks = ",".join("?" * len(event_ids))
    found = {}
    for row in db.execute(
        f"SELECT id,payload_json FROM plan_node_events WHERE id IN ({marks})", event_ids,
    ):
        payload = _decoded(row["payload_json"])
        if payload is not None:
            found[row["id"]] = payload
    return found


def _placed_raised_in(db, hold_ids: list[str], node_ids) -> dict[str, dict]:
    """Where each hold was raised, from its ``hold_placed`` event, found by the task's event index."""
    if not node_ids:
        return {}
    marks = ",".join("?" * len(node_ids))
    wanted = set(hold_ids)
    found: dict[str, dict] = {}
    for row in db.execute(
        f"SELECT payload_json FROM plan_node_events WHERE node_id IN ({marks}) AND event='hold_placed'",
        sorted(node_ids),
    ):
        payload = _decoded(row["payload_json"])
        if payload is None or payload.get("hold_id") not in wanted:
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
