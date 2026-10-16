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
    restated by re-blocking reads as the hold that ended its round: its clearance
    names its ``successor``, and the links are followed by primary key until a
    hold that was not restated. So an earlier card never claims an answer the
    user did not give, nor the state of a later ask the same task raised after
    that round was answered. A hold restated before successors were recorded
    names none; it falls back to the newest ask its task placed on the same
    artifact, which can misread a round that a later ask followed. A hold that
    is not a user ask, or does not exist, is absent.
    """
    ids = list(dict.fromkeys(hold_ids))
    if not ids:
        return {}
    rows = _ask_rows(db, ids)
    # holds.clear_write keeps the ending detail under its own ``detail`` key.
    answers = _endings(db, rows.values())
    placed, newest = _placed_on_tasks(db, set(rows), {row["node_id"] for row in rows.values()})
    ends = _round_ends(db, rows, answers, newest)
    return {
        hold_id: _ask_row(row, ends[hold_id], answers, placed)
        for hold_id, row in rows.items()
    }


def _ask_rows(db, hold_ids) -> dict[str, dict]:
    """The user asks among ``hold_ids``, by hold id, read by primary key."""
    ids = list(dict.fromkeys(hold_ids))
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {
        row["id"]: dict(row)
        for row in db.execute(
            f"""SELECT h.id, h.node_id, h.owner_id, h.clearance_event_id,
                       {open_user_block_sql('h')} AS is_open
                  FROM plan_node_holds h
                 WHERE h.id IN ({marks}) AND h.kind='blocked' AND h.owner_kind='user'
                   AND h.owner_id IS NOT NULL""",
            ids,
        )
    }


def _endings(db, rows) -> dict[str, dict]:
    """Each cleared hold's ending detail, by its clearance event id."""
    cleared = [row["clearance_event_id"] for row in rows if row["clearance_event_id"]]
    return {
        event_id: detail
        for event_id, payload in _event_payloads(db, cleared).items()
        if isinstance(detail := payload.get("detail"), dict)
    }


def _round_ends(db, rows: dict[str, dict], answers: dict, newest: dict) -> dict[str, dict]:
    """The hold that ended each requested hold's round, by requested hold id.

    Each step reads the next holds and their clearance events by primary key, in
    one batch for every chain still being followed, so the cost is the restatements
    behind the cards on screen, never the history of the task. A link to a hold
    already visited, or to none, stops at the hold reached so far. The endings it
    reads join ``answers``, so the caller reads each end hold's answer there.
    """
    ends: dict[str, dict] = {}
    at = dict(rows)
    seen = {hold_id: {hold_id} for hold_id in rows}
    while at:
        step = {}
        for start, row in at.items():
            successor = _successor(row, answers, newest)
            if successor is None or successor in seen[start]:
                ends[start] = row
            else:
                step[start] = successor
        found = _ask_rows(db, step.values())
        answers.update(_endings(db, found.values()))
        following = {}
        for start, successor in step.items():
            if successor in found:
                seen[start].add(successor)
                following[start] = found[successor]
            else:
                ends[start] = at[start]
        at = following
    return ends


def _successor(row, answers: dict, newest: dict) -> str | None:
    """The hold a restated ``row`` handed its round to, or None when it was not restated."""
    answer = answers.get(row["clearance_event_id"], {})
    if answer.get("ended") != "restated":
        return None
    successor = answer.get("successor")
    if isinstance(successor, str) and successor:
        return successor
    return newest.get((str(row["node_id"]), str(row["owner_id"])))


def _ask_row(row, end, answers: dict, placed: dict) -> dict:
    answer = answers.get(end["clearance_event_id"], {})
    return {
        "artifact_id": row["owner_id"],
        "open": bool(end["is_open"]),
        "raised_in": placed.get(row["id"], {}),
        "answer": {
            key: value for key, value in answer.items()
            if key in ("verdict", "feedback_counts", "ended")
        },
    }


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


def _placed_on_tasks(db, hold_ids, node_ids) -> tuple[dict[str, dict], dict[tuple[str, str], str]]:
    """Where each hold was raised, from its ``hold_placed`` event, found by the task's event index.

    The same read, in placement order, also yields the newest hold each task
    placed per owner: the fallback successor for a hold restated before
    successors were recorded, at no further read.
    """
    if not node_ids:
        return {}, {}
    marks = ",".join("?" * len(node_ids))
    wanted = set(hold_ids)
    found: dict[str, dict] = {}
    newest: dict[tuple[str, str], str] = {}
    for row in db.execute(
        f"""SELECT node_id,payload_json FROM plan_node_events
             WHERE node_id IN ({marks}) AND event='hold_placed'
             ORDER BY node_id,created_at,rowid""",
        sorted(node_ids),
    ):
        payload = _decoded(row["payload_json"])
        if payload is None or not isinstance(payload.get("hold_id"), str):
            continue
        if (
            (payload.get("kind"), payload.get("owner_kind")) == ("blocked", "user")
            and payload.get("owner_id") is not None
        ):
            newest[(str(row["node_id"]), str(payload["owner_id"]))] = payload["hold_id"]
        if payload["hold_id"] not in wanted:
            continue
        raised_in = payload.get("raised_in")
        found[payload["hold_id"]] = {
            key: value for key, value in raised_in.items() if isinstance(value, str) and value
        } if isinstance(raised_in, dict) else {}
    return found, newest


def _decoded(raw) -> dict | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None
