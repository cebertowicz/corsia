"""Build the hierarchical thread artifact pane: nodes and needs_attention.

The pane is a typed projection of artifacts only — no ``threads.spawned_by``
query, no subagent/session rows. Live worktree files collapse under a derived
worktree-group node (Git built in via the grouping-provider registry); every
visible row carries a backend-resolved presentation descriptor.

Assembling the pane is strictly read-only: it may read verified linked-worktree
Git metadata for grouping but spawns no Git process and mutates nothing, so a
GET never repairs. Safe repair apply is scheduled by the API layer *after* the
response (see ``api.registry``); when it mutates an identity the existing
``artifacts_changed`` broadcast makes the client refetch, so a repaired row
still disappears on its own. ``needs_attention`` is read from the durable
reconcile receipts — an attempt that failed against the identity version still
on record.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import infra.config as config
from infra.database import get_db
from infra.project_repo_policy import list_registered_repo_policies
from registry import grouping, presentation
from registry.doc_freshness import freshness
from registry.identity_bridge import (
    OWNER_REGISTRATION_STATES,
    PANE_REGISTRATION_STATES,
)
from registry.identity_metadata import parse_metadata
from registry.shared_principals import is_under_shared_roots, shared_roots
from registry.reconcile import apply_for_thread, preview_for_thread
from registry.retirement_predicate import (
    RETIREMENT_NOT_EXEMPT_CLAUSE,
    bind_shared_documents,
)


def pane_for_thread(thread_id: str) -> dict:
    """Assemble the hierarchical pane payload for one thread. Read-only."""
    db = get_db()
    try:
        arts = _artifacts_for_thread(db, thread_id)
        policies = list_registered_repo_policies(db=db)
        needs_attention = _needs_attention(db, thread_id)
        not_exempt_ids = _not_exempt_artifact_ids(db, thread_id)
    finally:
        db.close()

    cache: dict = {}
    visible = [
        (artifact, _artifact_item(artifact, not_exempt_ids)) for artifact in arts
    ]

    attention_ids = {item["artifact_id"] for item in needs_attention}
    groups: dict[str, dict] = {}
    group_sort: dict[str, str] = {}
    top_level: list[dict] = []
    for a, item in visible:
        assignment = grouping.group_for_path(a.get("path"), policies=policies, cache=cache)
        if assignment is None:
            top_level.append(item)
            continue
        group = groups.get(assignment.key)
        if group is None:
            group = {
                "kind": "worktree-group",
                "key": assignment.key,
                "label": assignment.label,
                "branch": assignment.branch,
                "file_count": 0,
                "attention": False,
                "children": [],
            }
            groups[assignment.key] = group
            group_sort[assignment.key] = ""
        group["children"].append(item)
        group["file_count"] += 1
        group["attention"] = group["attention"] or item["id"] in attention_ids
        group_sort[assignment.key] = max(group_sort[assignment.key], item["updated_at"] or "")

    # Pinned rows sort above the rest, recency ordering the rest and each tier.
    # A pinned file inside a worktree group leads its group rather than being
    # lifted out of it: the group is how the user reads those paths, and
    # promoting one child would strand it from the branch it belongs to.
    for group in groups.values():
        group["children"].sort(
            key=lambda c: (c.get("pinned", False), c.get("updated_at") or ""),
            reverse=True,
        )

    nodes = top_level + list(groups.values())
    nodes.sort(
        key=lambda n: (
            n.get("pinned", False),
            n.get("updated_at") or group_sort.get(n.get("key", ""), "") or "",
        ),
        reverse=True,
    )
    return {
        "nodes": nodes,
        "needs_attention": needs_attention,
        # One-release stale-client compatibility; no producer or History lane.
        "history": [],
    }


def _non_pane_type_exclusion() -> tuple[str, list[str]]:
    """Return the SQL clause and params excluding continuation-handle types.

    Shared by every pane query so the set of rows the pane may show has one
    definition rather than one per query.
    """
    clause = " AND ".join(
        "NOT (i.integration=? AND i.type=?)" for _ in presentation.NON_PANE_TYPES
    )
    params = [value for pair in presentation.NON_PANE_TYPES for value in pair]
    return clause, params


def _needs_attention(db, thread_id: str) -> list[dict]:
    """Return this thread's unresolved reconcile failures.

    A failed receipt stays unresolved only while it still describes the
    identity's *current* version and that identity is still active: a repaired,
    retired, or otherwise changed artifact drops out without any filesystem
    probe on the read path.

    Constrained to rows this thread's pane can actually show. Reconcile scans
    every owner-scoped identity regardless of surfacing, so without the same
    predicate the pane uses, a quiet or demoted artifact could raise an
    attention entry that the client has no row to name — rendering as an
    un-actionable literal "Artifact".
    """
    states = PANE_REGISTRATION_STATES
    placeholders = ",".join("?" for _ in states)
    exclude_clause, exclude_params = _non_pane_type_exclusion()
    rows = db.execute(
        f"""SELECT r.artifact_id, r.reason
           FROM artifact_reconcile_receipts r
           JOIN artifact_identities i ON i.id = r.artifact_id
           JOIN artifact_registrations reg ON reg.artifact_id = i.id
           WHERE r.thread_id=?
             AND r.status='failed'
             AND i.state='active'
             AND i.updated_at = r.identity_version
             AND reg.parent_thread_id = r.thread_id
             AND reg.relationship_state IN ({placeholders})
             AND reg.surfacing='visible'
             AND {exclude_clause}
           ORDER BY r.created_at DESC, r.id ASC""",
        (thread_id, *states, *exclude_params),
    ).fetchall()
    return [
        {
            "kind": "repair-failure",
            "artifact_id": row["artifact_id"],
            "reason": row["reason"],
            "retryable": True,
        }
        for row in rows
    ]


def _not_exempt_artifact_ids(db, thread_id: str) -> set[str]:
    """Artifact ids for this thread whose registration is retirement-eligible.

    One SQL query per pane fetch reusing the single retirement-exemption
    predicate (registry.retirement_predicate.RETIREMENT_NOT_EXEMPT_CLAUSE)
    instead of a hand-copied Python mirror.
    """
    bind_shared_documents(db)
    where, where_params = _pane_visible_where()
    rows = db.execute(
        f"""SELECT r.artifact_id FROM artifact_registrations AS r
            JOIN artifact_identities AS i ON i.id=r.artifact_id
            WHERE {where} AND {RETIREMENT_NOT_EXEMPT_CLAUSE}""",
        (thread_id, *where_params),
    ).fetchall()
    return {row["artifact_id"] for row in rows}


def _artifact_item(a: dict, not_exempt_ids: set[str]) -> dict:
    meta = _metadata(a.get("metadata"))
    descriptor = presentation.resolve_descriptor(a["integration"], a["type"])
    disabled_reason = (
        None
        if descriptor.capability != presentation.CAPABILITY_UNSUPPORTED
        else "No handler is registered for this artifact type."
    )
    raw_name = a.get("title") or a.get("external_id")
    return {
        "kind": "artifact",
        "id": a["id"],
        "integration": a["integration"],
        "type": a["type"],
        "external_id": a["external_id"],
        "title": a.get("title"),
        "name": _safe_name(raw_name, "Untitled artifact"),
        "path": a.get("path"),
        "retention": a["retention"],
        "surfacing": a["surfacing"],
        "pinned": a["relationship_state"] == "owner-pinned",
        "pin_eligible": a["relationship_state"] in OWNER_REGISTRATION_STATES,
        "retirement_exempt": a["id"] not in not_exempt_ids,
        "state": a["state"],
        "updated_at": a.get("updated_at"),
        "description": a.get("description"),
        "presentation": {
            "label": descriptor.label,
            "icon": descriptor.icon,
            "capability": descriptor.capability,
        },
        "action": {
            "capability": descriptor.capability,
            "disabled": descriptor.capability == presentation.CAPABILITY_UNSUPPORTED,
            "disabled_reason": disabled_reason,
        },
        "metadata": meta,
    }


# History is intentionally not produced; the response keeps an empty key for compatibility.


def _pane_visible_where(
    registration_states: tuple[str, ...] = PANE_REGISTRATION_STATES,
) -> tuple[str, list]:
    """The one WHERE clause that decides which registrations the pane shows.

    Shared by the live pane and the checkpoint snapshot so the frozen set
    cannot drift from the pane it mirrors. The first parameter is the thread.
    """
    placeholders = ",".join("?" for _ in registration_states)
    exclude_clause, exclude_params = _non_pane_type_exclusion()
    clause = (
        "r.parent_thread_id=? AND i.state='active' AND r.surfacing='visible' "
        f"AND r.relationship_state IN ({placeholders}) AND {exclude_clause}"
    )
    return clause, [*registration_states, *exclude_params]


def _artifacts_for_thread(
    db,
    thread_id: str,
    *,
    registration_states: tuple[str, ...] = PANE_REGISTRATION_STATES,
) -> list[dict]:
    """Return only active, visible registrations rendered by the live pane."""
    from registry.identity_bridge import public_row_from_normalized

    where, where_params = _pane_visible_where(registration_states)
    rows = db.execute(
        f"""SELECT i.id AS identity_id, i.integration, i.type, i.external_id,
                  i.title, i.description, i.path, i.state, i.retention,
                  i.metadata,
                  i.created_at, i.updated_at,
                  r.parent_thread_id, r.surfacing, r.metadata AS registration_metadata,
                  r.updated_at AS registration_updated_at,
                  r.relationship_state
           FROM artifact_identities i
           JOIN artifact_registrations r ON r.artifact_id = i.id
           WHERE {where}
           ORDER BY r.updated_at DESC, i.id ASC""",
        (thread_id, *where_params),
    ).fetchall()
    return [public_row_from_normalized(row) for row in rows]


def checkpoint_pane_rows(db, thread_id: str) -> list[dict]:
    """Return one thread's checkpoint artifact rows, capture-shaped.

    The set is what the pane shows (the shared ``_pane_visible_where``
    predicate) plus the overdue shared documents the thread owns whose row is
    settled, so it can hold more rows than the pane: ownership adds no pane
    row, but an owner is still told what it owes a review, whatever hid the
    row. Only the order differs, pinned rows first then
    most recently
    updated, so a cut keeps the rows the owner pinned — selected for the
    checkpoint snapshot
    (#1602). One addition the live pane's public shape does not carry:
    ``origin_plan_node_id``, read only in this capture path per the in-lane
    decision, so ``public_row_from_normalized`` and the pane stay unchanged.
    """
    where, where_params = _pane_visible_where()
    owned = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    rows = db.execute(
        f"""SELECT i.id, i.integration, i.type, i.external_id,
                  i.title, i.path, i.metadata, r.surfacing,
                  r.relationship_state, r.origin_plan_node_id,
                  r.updated_at AS registration_updated_at
           FROM artifact_identities i
           JOIN artifact_registrations r ON r.artifact_id = i.id
           WHERE ({where})
              OR (r.parent_thread_id=? AND i.state='active'
                  AND r.surfacing='settled'
                  AND r.relationship_state IN ({owned}))
           ORDER BY r.updated_at DESC, i.id ASC""",
        (thread_id, *where_params, thread_id, *OWNER_REGISTRATION_STATES),
    ).fetchall()
    now = datetime.now(timezone.utc)
    roots = shared_roots(workspace=config.WORKSPACE, db=db)
    result = []
    for raw in rows:
        row = dict(raw)
        hidden = row.pop("surfacing") != "visible"
        path = row["path"] or row["external_id"]
        if (
            row["relationship_state"] in OWNER_REGISTRATION_STATES
            and path
            and is_under_shared_roots(path, roots)
        ):
            metadata = parse_metadata(row["metadata"])
            row["update_trigger"] = metadata.get("update_trigger")
            row["freshness_state"] = freshness(path, metadata, now)["state"]
        if hidden and row.get("freshness_state") != "overdue":
            continue
        result.append(row)
    result.sort(key=lambda row: row.get("freshness_state") == "overdue", reverse=True)
    result.sort(
        key=lambda row: row["relationship_state"] == "owner-pinned",
        reverse=True,
    )
    return result


def reconcile_for_thread(thread_id: str, *, mode: str = "preview") -> dict:
    """Return a reconcile result for one thread: ``preview`` (dry-run, the
    default) or ``apply`` (idempotent repair)."""
    if mode == "apply":
        return apply_for_thread(thread_id)
    if mode != "preview":
        raise ValueError(f"unknown reconcile mode: {mode!r}")
    return preview_for_thread(thread_id)


def _metadata(raw: object) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE,
)


def _safe_name(raw: str | None, fallback: str) -> str:
    """Return a display-safe artifact name — never a raw path or opaque ID.

    Strips filesystem paths to their basename. Rejects bare UUID/UUIDv7
    external IDs (which carry no human meaning) and falls back to the
    caller-supplied label.
    """
    if not raw:
        return fallback
    if _UUID_RE.match(raw):
        return fallback
    if raw.startswith("/") or raw.startswith("~") or (
        len(raw) > 2 and raw[1] == ":" and raw[2] in "/\\"
    ):
        base = os.path.basename(raw.rstrip("/\\"))
        return base if base else fallback
    return raw
