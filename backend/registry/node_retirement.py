"""Registration-grain retirement: a row's reason to hold pane space has ended.

Four causes retire a row, through one writer: the plan node it was registered
under settles, the artifact itself ends (a pull request merges or closes), the
row exists only because ownership moved, or the row was registered under no
task and its thread has since completed a later one. All stamp the same permanent
``retired_at`` fact that pin, unpin and re-registration read, append an
audited event, and share one after-commit invalidation and telemetry path.
"""

from __future__ import annotations

from collections import defaultdict
import json
from typing import Iterable

from registry.identity_bridge import PANE_REGISTRATION_STATES
from registry.presentation import NON_PANE_TYPES
from registry.retirement_predicate import RETIREMENT_NOT_EXEMPT_CLAUSE
from registry.change_notifications import build_artifact_change_messages
from registry.registration_events import (
    _append_registration_events,
    _now,
    routing_registrations,
)


#: The registration events that record a retirement, one per cause.
NODE_RETIRED_EVENT = "origin_node_retired"
ARTIFACT_ENDED_EVENT = "artifact_ended"
OWNERSHIP_ROW_RETIRED_EVENT = "ownership_row_retired"
TASKLESS_ROW_RETIRED_EVENT = "taskless_row_retired"
_RETIREMENT_EVENTS = (
    NODE_RETIRED_EVENT, ARTIFACT_ENDED_EVENT, OWNERSHIP_ROW_RETIRED_EVENT,
    TASKLESS_ROW_RETIRED_EVENT,
)
_RETIREMENT_EVENT_PLACEHOLDERS = ",".join("?" for _ in _RETIREMENT_EVENTS)

# A pinned row is still eligible: retired_at is the permanent record of
# whether this row was retired, independent of whether pin is currently
# overriding what the pane shows for it.
_UNRETIRED_WHERE = f"r.retired_at IS NULL AND {RETIREMENT_NOT_EXEMPT_CLAUSE}"
# Ownership is a fact about accountability, not about the pane, so a row that
# exists only because ownership moved retires even when its artifact is exempt
# from task settlement.
_UNRETIRED_ANY_WHERE = "r.retired_at IS NULL"
# A registration made while its thread had no task selected, last touched
# before a given moment, of a type the pane can show. Compared through
# ``julianday`` because two timestamp formats exist in stored rows.
_NON_PANE_PAIRS = tuple(sorted(NON_PANE_TYPES))
_TASKLESS_BEFORE = (
    "r.parent_thread_id=? AND r.origin_plan_node_id IS NULL "
    "AND julianday(r.updated_at) < julianday(?) "
    "AND NOT EXISTS (SELECT 1 FROM artifact_identities AS t "
    "WHERE t.id=r.artifact_id AND ("
    + " OR ".join("(t.integration=? AND t.type=?)" for _ in _NON_PANE_PAIRS)
    + "))"
)
_NON_PANE_PARAMS = tuple(value for pair in _NON_PANE_PAIRS for value in pair)


def capture_event_floor(db) -> int:
    """Mark the start of one transaction's possible registration events."""
    return int(
        db.execute(
            "SELECT COALESCE(MAX(rowid),0) FROM artifact_registration_events"
        ).fetchone()[0]
    )


def retire_registrations_for_node(
    db,
    *,
    node_id: str,
    trigger: str,
    causal_event_id: str | None,
) -> int:
    """Stamp and audit every unsettled task-scoped registration for one settled node."""
    return _retire(
        db,
        scope="r.origin_plan_node_id=?",
        scope_value=node_id,
        event_type=NODE_RETIRED_EVENT,
        authority="plan-node-settled",
        source_path="registry.node_retirement.retire_registrations_for_node",
        trigger=trigger,
        causal_event_id=causal_event_id,
    )


def retire_registrations_for_ended_artifact(
    db,
    *,
    artifact_id: str,
    trigger: str,
    causal_event_id: str | None,
) -> int:
    """Stamp and audit every unretired registration of one artifact that has ended.

    The caller records the terminal state on the identity first, in the same
    transaction, so the artifact is no longer exempt when this reads it.
    """
    return _retire(
        db,
        scope="r.artifact_id=?",
        scope_value=artifact_id,
        event_type=ARTIFACT_ENDED_EVENT,
        authority="artifact-ended",
        source_path=(
            "registry.node_retirement.retire_registrations_for_ended_artifact"
        ),
        trigger=trigger,
        causal_event_id=causal_event_id,
    )


def retire_registration_by_id(
    db,
    *,
    registration_id: str,
    trigger: str,
    causal_event_id: str | None = None,
) -> int:
    """Stamp and audit one brand new registration of an already-ended artifact.

    There is no settlement or merge event left to fire for a row created
    after its artifact already ended, so this gives it the same stamp and
    audited event as any other retirement instead of a silent insert-time
    exception. The caller has no after-commit nudge path, so this does not
    also emit ``artifact_pane.auto_retired`` telemetry.
    """
    return _retire(
        db,
        scope="r.id=?",
        scope_value=registration_id,
        event_type=ARTIFACT_ENDED_EVENT,
        authority="artifact-ended",
        source_path="registry.node_retirement.retire_registration_by_id",
        trigger=trigger,
        causal_event_id=causal_event_id,
    )


def retire_ownership_row(
    db,
    *,
    artifact_id: str,
    thread_id: str,
    trigger: str,
    causal_event_id: str | None = None,
) -> int:
    """Stamp and audit one thread's row that only an ownership change put there.

    The caller decides the row is ownership-only (it was created, or became
    pane-eligible, inside the ownership write itself). Pin or any later
    registration by that thread brings it back.
    """
    return _retire(
        db,
        scope="r.artifact_id=? AND r.parent_thread_id=?",
        scope_value=(artifact_id, thread_id),
        event_type=OWNERSHIP_ROW_RETIRED_EVENT,
        authority="ownership-change",
        source_path="registry.node_retirement.retire_ownership_row",
        trigger=trigger,
        causal_event_id=causal_event_id,
        spare_exempt=False,
    )


def retire_taskless_rows(
    db,
    *,
    thread_id: str,
    started_at: str,
    trigger: str,
    causal_event_id: str | None = None,
) -> int:
    """Stamp and audit a thread's taskless rows last touched before ``started_at``.

    The caller passes the moment the thread started a task it has now
    completed: rows it registered with no task selected, and has not touched
    since, are no longer what it is working on. Exempt rows stay, and pin or
    the thread's next registration brings a row back.
    """
    return _retire(
        db,
        scope=_TASKLESS_BEFORE,
        scope_value=(thread_id, started_at, *_NON_PANE_PARAMS),
        event_type=TASKLESS_ROW_RETIRED_EVENT,
        authority="plan-node-settled",
        source_path="registry.node_retirement.retire_taskless_rows",
        trigger=trigger,
        causal_event_id=causal_event_id,
    )


def pane_threads(db, artifact_id: str) -> set[str]:
    """Threads holding a pane-eligible row for this artifact.

    Compared with itself before and after one ownership write, never used as
    the pane predicate (``registry.view._pane_visible_where``): it ignores the
    identity's state and type, which that write does not change for a row.
    """
    placeholders = ",".join("?" for _ in PANE_REGISTRATION_STATES)
    rows = db.execute(
        "SELECT parent_thread_id FROM artifact_registrations "
        "WHERE artifact_id=? AND surfacing='visible' "
        f"AND relationship_state IN ({placeholders})",
        (artifact_id, *PANE_REGISTRATION_STATES),
    ).fetchall()
    return {str(row["parent_thread_id"]) for row in rows}


def retire_rows_ownership_added(
    db,
    artifact_id: str,
    shown_before: set[str],
    *,
    keep: str | None,
    trigger: str,
    causal_event_id: str | None,
) -> None:
    """Keep an ownership write from changing what any pane shows.

    Ownership decides who is accountable, not what a thread is working on, so
    a row this write created or made pane-eligible retires at once through the
    shared writer. ``keep`` is the one thread a deliberate hand-off shows the
    artifact to.
    """
    for thread_id in sorted(pane_threads(db, artifact_id) - shown_before):
        if thread_id == keep:
            continue
        retire_ownership_row(
            db,
            artifact_id=artifact_id,
            thread_id=thread_id,
            trigger=trigger,
            causal_event_id=causal_event_id,
        )


def _retire(
    db,
    *,
    scope: str,
    scope_value: str | tuple[str, ...],
    event_type: str,
    authority: str,
    source_path: str,
    trigger: str,
    causal_event_id: str | None,
    spare_exempt: bool = True,
) -> int:
    """Stamp, settle unpinned visible rows, and audit one retirement cause.

    ``spare_exempt`` chooses whether exempt rows are left alone: an ownership
    retirement ignores the exemption.
    """
    where = f"{scope} AND {_UNRETIRED_WHERE if spare_exempt else _UNRETIRED_ANY_WHERE}"
    scope_params = (scope_value,) if isinstance(scope_value, str) else scope_value
    rows = db.execute(
        f"""SELECT r.id,r.artifact_id,r.parent_thread_id,r.relationship_state,
                  r.surfacing
             FROM artifact_registrations AS r
            WHERE {where}
            ORDER BY r.id""",
        scope_params,
    ).fetchall()
    if not rows:
        return 0
    now = _now()
    db.execute(
        f"""UPDATE artifact_registrations AS r
              SET retired_at=?,
                  surfacing=CASE
                      WHEN relationship_state<>'owner-pinned' AND surfacing='visible'
                          THEN 'settled'
                      ELSE surfacing
                  END,
                  updated_at=?
            WHERE {where}""",
        (now, now, *scope_params),
    )
    _append_registration_events(
        db,
        (
            {
                "artifact_id": row["artifact_id"],
                "event_type": event_type,
                "actor_thread_id": None,
                "authority": authority,
                "reason": None,
                "source_path": source_path,
                "causal_event_id": causal_event_id,
                "affected_thread_id": row["parent_thread_id"],
                "old_owner_thread_id": None,
                "new_owner_thread_id": None,
                "old_relationship_state": row["relationship_state"],
                "new_relationship_state": row["relationship_state"],
                "metadata": {
                    "trigger": trigger,
                    "old_surfacing": row["surfacing"],
                    "new_surfacing": (
                        "settled"
                        if row["surfacing"] == "visible"
                        and row["relationship_state"] != "owner-pinned"
                        else row["surfacing"]
                    ),
                },
                "now": now,
                "idempotency_scope": row["id"],
            }
            for row in rows
        ),
    )
    return len(rows)


def after_commit(floor: int | None, *, db) -> tuple[tuple[str, dict], ...]:
    """Build exact committed artifact invalidations and emit aggregate evidence."""
    if floor is None or db.in_transaction:
        return ()
    rows = db.execute(
        f"""SELECT e.artifact_id,e.affected_thread_id,e.metadata,
                  i.integration,i.type,i.external_id,t.user_id
             FROM artifact_registration_events e
             JOIN artifact_identities i ON i.id=e.artifact_id
             JOIN threads t ON t.id=e.affected_thread_id
            WHERE e.rowid>? AND e.event_type IN ({_RETIREMENT_EVENT_PLACEHOLDERS})
            ORDER BY e.rowid""",
        (floor, *_RETIREMENT_EVENTS),
    ).fetchall()
    affected: dict[str, set[str]] = defaultdict(set)
    identities: dict[str, tuple[str, str, str]] = {}
    telemetry: dict[tuple[str, str], list] = defaultdict(list)
    for row in rows:
        if not row["affected_thread_id"] or not row["user_id"]:
            continue
        affected[row["artifact_id"]].add(row["affected_thread_id"])
        identities[row["artifact_id"]] = (
            row["integration"], row["type"], row["external_id"],
        )
        metadata = json.loads(row["metadata"] or "{}")
        key = (row["affected_thread_id"], metadata.get("trigger", "archived"))
        telemetry[key].append((row, metadata))
    messages: list[tuple[str, dict]] = []
    for artifact_id, thread_ids in sorted(affected.items()):
        routing = routing_registrations(*identities[artifact_id], db=db)
        owner = routing.get("owner")
        owner_thread_id = str(owner["parent_thread_id"]) if owner else None
        messages.extend(build_artifact_change_messages(
            artifact_id,
            owner_thread_id=owner_thread_id,
            affected_thread_ids=sorted(thread_ids),
            db=db,
        ))
    _emit_retirement_telemetry(telemetry)
    return tuple(messages)


def _emit_retirement_telemetry(groups) -> None:
    from infra.product_events import emit_product_event

    for (thread_id, trigger), entries in groups.items():
        visible = 0
        quiet = 0
        type_counts: dict[str, int] = defaultdict(int)
        for row, metadata in entries:
            if metadata.get("old_surfacing") == "visible":
                visible += 1
            elif metadata.get("old_surfacing") == "quiet":
                quiet += 1
            type_counts[f"{row['integration']}/{row['type']}"] += 1
        emit_product_event(
            "artifact_pane",
            "auto_retired",
            thread_id=thread_id,
            properties={
                "trigger": trigger,
                "retired_count": visible,
                "quiet_stamped_count": quiet,
                "type_counts": dict(type_counts),
                "contract_version": 1,
            },
        )
