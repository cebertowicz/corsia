"""Frozen install-time snapshots of a thread's artifacts and plan status.

One install writes three once-frozen columns atomically (#1602): the
continuity snapshot (owned elsewhere) plus, from here, the thread's
owned-and-followed visible artifact rows and its bounded plan status. The
point is the first turn after a fold already knows the thread's files and
plan without a tool call.

An already-owned caller transaction captures inside the checkpoint transaction,
against that transaction's connection, so all three columns freeze at one
commit. Normal installs and clean caller-provided connections preflight the
artifact pane before their writer begins, then carry those bytes into the
atomic checkpoint update; the plan projection remains in the writer. Both
captures are best-effort by section: the artifact query and the plan projection
belong to other lanes, and a failure in either must not block the checkpoint's
own install. A failed section stores
``CAPTURE_FAILED_JSON`` so the model reads "unavailable" instead of silence
(a NULL column means only a pre-change row), and the warning log records the
distinct reason.

Bounds follow the in-lane decisions of 2026-09-24: at most 20 artifact rows
under the ``_cut`` pattern (first row always kept, then an "N more omitted"
count), one locator field (path, else external id), and the plan view is
``plans.status_context``'s thin projection under its own byte budget.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from core.checkpoint_snapshot_render import CAPTURE_FAILED_JSON
from plans.public import bounded_label as bounded, PlanError, cut_rows
from registry.identity_bridge import OWNER_REGISTRATION_STATES

log = logging.getLogger(__name__)

#: The failures a capture degrades on: the store or the plan read refusing.
#: Anything else is a programming error and fails the install loudly.
_EXPECTED_CAPTURE_FAILURES = (sqlite3.Error, PlanError, PermissionError)

#: At most 20 artifact rows in one frozen snapshot, under its own serialized
#: byte cap — the same order of magnitude as the live plan status's Current
#: section (``plans.status.CURRENT_BYTES``), so the frozen prefix stays small
#: next to the narrative and continuity entries it joins.
ARTIFACT_ROWS = 20
ARTIFACT_BYTES = 3584
TITLE_MAX_BYTES = 96
LOCATOR_MAX_BYTES = 96
TRIGGER_MAX_CHARS = 60


def capture_checkpoint_snapshots(
    connection: sqlite3.Connection, thread_id: str, *, thread_row: sqlite3.Row,
    artifact_snapshot_json: str | None = None,
) -> tuple[str, str]:
    """Return the artifact and plan-status snapshot JSON for one install.

    Each value is stored verbatim in its column: the artifact snapshot is
    always a JSON object (``{"rows": [...], "omitted_count": N,
    "total_count": M}``, empty set included), and the plan snapshot is the
    frozen ``plans.status_context`` projection, or the JSON literal ``null``
    when the thread has no plan. A failed capture (an expected store or plan
    error) returns ``CAPTURE_FAILED_JSON`` for its column; other errors
    propagate.
    """
    artifact_json = (
        artifact_snapshot_json
        if artifact_snapshot_json is not None
        else capture_artifact_snapshot(connection, thread_id)
    )
    try:
        plan_json = _plan_status_snapshot(
            connection, thread_id, thread_row,
        )
    except _EXPECTED_CAPTURE_FAILURES:
        log.warning(
            "checkpoint plan-status snapshot capture failed for thread %s",
            thread_id, exc_info=True,
        )
        plan_json = CAPTURE_FAILED_JSON
    return artifact_json, plan_json


def capture_artifact_snapshot(
    connection: sqlite3.Connection, thread_id: str,
) -> str:
    """Capture the artifact pane, degrading only expected external failures."""
    try:
        return _artifact_snapshot(connection, thread_id)
    except _EXPECTED_CAPTURE_FAILURES:
        log.warning(
            "checkpoint artifact snapshot capture failed for thread %s",
            thread_id, exc_info=True,
        )
        return CAPTURE_FAILED_JSON


def _artifact_snapshot(connection: sqlite3.Connection, thread_id: str) -> str:
    """Freeze the thread's pane row set, bounded, as one JSON object."""
    from registry.view import checkpoint_pane_rows

    rows = [_artifact_row(raw) for raw in checkpoint_pane_rows(connection, thread_id)]
    kept = cut_rows(rows, ARTIFACT_ROWS, ARTIFACT_BYTES, _minimal_artifact_row)
    return json.dumps(
        {
            "rows": kept,
            "omitted_count": len(rows) - len(kept),
            "total_count": len(rows),
        },
        sort_keys=True,
    )


def _plan_status_snapshot(
    connection: sqlite3.Connection, thread_id: str, thread_row: sqlite3.Row,
) -> str:
    """Freeze the actor's thin plan status, as ``plans.status_context`` renders it.

    That module owns the projection a checkpoint or context assembly delivers
    (its own byte budget, paused and nothing-ready handling), so the frozen
    view and the passive-digest line cannot disagree; its ``as_of`` clock is
    the capture moment.
    """
    from plans.public import PlanActor, read_checkpoint_status, render_status as render

    actor = PlanActor(thread_id, str(thread_row["user_id"]), thread_row["project_id"])
    status = read_checkpoint_status(actor=actor, db=connection)
    return render(status) if status is not None else "null"


def _artifact_row(raw: dict) -> dict:
    """Shape one capture row: identity, one locator, ownership, plan origin."""
    title = raw["title"] or raw["external_id"] or "Untitled artifact"
    row = {
        "id": str(raw["id"]),
        "title": bounded(str(title), TITLE_MAX_BYTES),
        "integration": str(raw["integration"]),
        "type": str(raw["type"]),
        "locator": bounded(str(_locator(raw)), LOCATOR_MAX_BYTES),
        "owns": raw["relationship_state"] in OWNER_REGISTRATION_STATES,
        "origin_plan_node_id": raw["origin_plan_node_id"],
    }
    if "update_trigger" in raw:
        row["trigger"] = _clip_trigger(raw.get("update_trigger"))
        if raw.get("freshness_state") in {"overdue", "never_verified"}:
            row["freshness"] = raw["freshness_state"]
    return row


def _clip_trigger(value: object) -> str:
    text = " ".join(str(value or "").splitlines()).strip()
    return text if len(text) <= TRIGGER_MAX_CHARS else text[:TRIGGER_MAX_CHARS] + "…"


def _minimal_artifact_row(row: dict) -> dict:
    """The smallest truthful artifact row: identity alone, marked truncated."""
    return {
        "id": row["id"], "integration": row["integration"], "type": row["type"],
        "truncated": True,
    }


def _locator(raw: dict) -> str:
    """One locator, by precedence: path, else external id."""
    return str(raw["path"] or raw["external_id"])
