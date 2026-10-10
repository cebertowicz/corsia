"""Batched all-state review facts for explicit resolved owner scopes."""
from __future__ import annotations

from typing import Any

from plans import closure, lesson_reviews, scheduled_wait
from plans.node_states import _TERMINAL_STATES


def _project(row, reason: str | None, *, settled_at: str | None,
             scheduled_for: str | None) -> dict[str, Any]:
    """Project already-prefetched batch facts without further database reads."""
    kind, _, identity = str(row["key"]).partition(":")
    subject, separator, sha = identity.rpartition(":")
    superseded = {
        lesson_reviews.SUPERSEDED_BY_VERSION: "version",
        lesson_reviews.SUPERSEDED_BY_TRIAL: "trial",
        lesson_reviews.SUPERSEDED_BY_CAPTURE: "capture",
        lesson_reviews.SUPERSEDED_BY_OWNER: "owner",
    }
    return {
        "kind": kind, "subject_id": subject if separator else None,
        "subject_hash": sha if separator else None, "key": row["key"],
        "node_id": row["node_id"], "plan_id": row["plan_id"],
        "owner_kind": row["owner_kind"], "owner_id": row["owner_id"],
        "state": row["state"], "open": bool(row["is_open"]), "archived_at": row["archived_at"],
        "plan_state": row["plan_state"], "owner_missing": bool(row["owner_missing"]),
        "close_reason": reason, "superseded": superseded.get(reason),
        "opened_at": row["opened_at"], "settled_at": settled_at,
        "artifact_id": row["artifact_id"], "linked_at": row["linked_at"],
        "scheduled_for": scheduled_for if row["is_open"] else None,
    }


def _batch_history_query(owner_count: int) -> str:
    owners = ",".join("?" for _ in range(owner_count))
    return f"""
    WITH scoped_links AS (
        SELECT n.id AS node_id, n.plan_id, n.state, n.archived_at, n.created_at AS opened_at,
               p.owner_kind, p.owner_id, p.state AS plan_state,
               t.id IS NULL AS owner_missing, l.source_event_id AS key,
               l.artifact_id, l.created_at AS linked_at,
               ({lesson_reviews._open_review_sql('n')}) AS is_open,
               ROW_NUMBER() OVER (PARTITION BY n.id ORDER BY l.created_at,l.artifact_id,l.role) AS ordinal
        FROM plans p
        JOIN plan_nodes n ON n.plan_id=p.id
        JOIN plan_node_artifact_links l ON l.node_id=n.id AND l.provenance=?
        LEFT JOIN threads t ON p.owner_kind='thread' AND t.id=p.owner_id
        WHERE p.owner_kind='thread' AND p.owner_id IN ({owners})
          AND (substr(l.source_event_id,1,?)=? OR substr(l.source_event_id,1,?)=?)
    )
    SELECT * FROM scoped_links WHERE ordinal=1
    """


def read_batch(db, *, owner_thread_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    """Project report history for explicit resolved owners without a user join or cursor."""
    if not isinstance(owner_thread_ids, tuple) or not owner_thread_ids:
        raise ValueError("a nonempty tuple of resolved owner scopes is required")
    if any(not isinstance(owner, str) or not owner or owner == "all" for owner in owner_thread_ids):
        raise ValueError("resolved owner scopes must be nonempty thread ids")
    owners = tuple(dict.fromkeys(owner_thread_ids))
    rows = []
    for offset in range(0, len(owners), 500):
        chunk = owners[offset:offset + 500]
        rows.extend(db.execute(_batch_history_query(len(chunk)), (
            lesson_reviews.PROVENANCE, *chunk,
            len(lesson_reviews.LESSON_PREFIX) + 1, f"{lesson_reviews.LESSON_PREFIX}:",
            len(lesson_reviews.CAPTURE_PREFIX) + 1, f"{lesson_reviews.CAPTURE_PREFIX}:",
        )).fetchall())
    owner_order = {owner: index for index, owner in enumerate(owners)}
    rows.sort(key=lambda row: (owner_order[str(row["owner_id"])], row["linked_at"], row["node_id"]))
    reasons: dict[str, str] = {}
    settled: dict[str, str] = {}
    scheduled: dict[str, str] = {}
    states_by_id = {str(row["node_id"]): str(row["state"]) for row in rows}
    node_ids = tuple(states_by_id)
    states = tuple(sorted(_TERMINAL_STATES))
    for offset in range(0, len(node_ids), 500):
        chunk = node_ids[offset:offset + 500]
        if not chunk:
            continue
        reasons.update(closure.close_reasons(db, [
            {"id": node_id, "state": states_by_id[node_id]} for node_id in chunk
        ]))
        marks = ",".join("?" for _ in chunk)
        for row in db.execute(
            f"SELECT node_id,created_at FROM plan_node_events WHERE node_id IN ({marks}) "
            f"AND event='state_transition' AND new_state IN ({','.join('?' for _ in states)}) "
            "ORDER BY node_id,created_at DESC,rowid DESC",
            (*chunk, *states),
        ).fetchall():
            settled.setdefault(str(row["node_id"]), str(row["created_at"]))
        for row in db.execute(
            f"SELECT plan_node_id,condition_value FROM expected_event_watches WHERE plan_node_id IN ({marks}) "
            "AND status='armed' AND condition_type='deadline' AND expected_event=? "
            "ORDER BY plan_node_id,armed_at DESC,rowid DESC",
            (*chunk, scheduled_wait.SCHEDULED_EVENT),
        ).fetchall():
            scheduled.setdefault(str(row["plan_node_id"]), str(row["condition_value"]))
    return [_project(
        row, reasons.get(str(row["node_id"])),
        settled_at=settled.get(str(row["node_id"])),
        scheduled_for=scheduled.get(str(row["node_id"])),
    ) for row in rows]
