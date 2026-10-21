"""Typed renderable detail on the one user-attention queue.

A decision is a first-class item in the user's queue of things that need them;
a plan may be *blocked on* it, but the plan does not own or render it. So the
queue is the existing `registry.attention` pile read user-globally, and each row
carries enough typed detail to be drawn **without a plan read and without
knowing which plan target the user is currently looking at**.

This replaces `plans.attention_routing`, which asserted "Plans renders this"
from the artifact *type* while the only renderer was a component mounted inside
one mutually-exclusive route. Here the claim and the payload travel together: a
row is routed to a typed renderer only when this module could actually build
that renderer's payload, so a generic list never hides an item nothing draws.

Canonical order (PRD-PLANS E8): `(priority, raised_at, external_id)`. `priority`
is the agent-settable `order_key`, so an agent can still reorder its own queued
decisions before the user reaches them.
"""

from __future__ import annotations

import plans.artifact_links as artifact_links
import plans.decision_projection as decision_projection

DECISION = "decision"


def _identity_key(row) -> tuple[str, str, str]:
    return (row["integration"], row["type"], row["external_id"])


# --- source attribution ------------------------------------------------------


def _sources(db, thread_ids: set[str]) -> dict[str, dict]:
    """Resolve the source thread label, project, and review tier (PRD-PLANS E1)."""
    if not thread_ids:
        return {}
    marks = ",".join("?" for _ in thread_ids)
    rows = db.execute(
        f"""SELECT s.id, s.title, s.project_id, s.archived_at, s.spawned_by,
                   COALESCE(s.lifecycle_state,'active') AS lifecycle,
                   p.name AS project_name
              FROM threads s
              LEFT JOIN projects p ON p.id = s.project_id
             WHERE s.id IN ({marks})""",
        tuple(sorted(thread_ids)),
    ).fetchall()
    return {row["id"]: _source(row) for row in rows}


def _source(row) -> dict:
    return {
        "thread_id": row["id"],
        "thread_label": row["title"] or "Untitled thread",
        "project_id": row["project_id"],
        "project_label": row["project_name"],
        # A thread with no spawner has no parent tier, so `decision_authz`
        # could not have required a second actor. Say so rather than letting
        # the chip imply a compliance review that never happened.
        "parent_reviewed": row["spawned_by"] is not None,
    }


_UNKNOWN_SOURCE = {
    "thread_id": None,
    "thread_label": "Unknown thread",
    "project_id": None,
    "project_label": None,
    "parent_reviewed": False,
}


# --- typed details -----------------------------------------------------------


def _decision_detail(
    row, body: dict, source: dict | None, answerable: bool,
) -> dict | None:
    """The full decision chip payload, or None when it must not be offered.

    ``answerable`` is the read-side half of the queue invariant: a row is drawn
    only when the answer route could actually settle it. A decision whose owning
    plan has gone away is withdrawn at archive time by
    ``plans.decision_lifecycle``; this guard means one that escaped that hook
    still cannot become the head and starve every later decision.
    """
    projected = decision_projection.project(row, body)
    if projected["state"] != decision_projection.RAISED or not answerable:
        return None
    payload = {field: projected[field] for field in decision_projection.WIRE_FIELDS}
    payload["source"] = source or {**_UNKNOWN_SOURCE, "thread_id": projected["thread_id"]}
    return payload


# --- batch lookups, bounded by the raised rows -------------------------------


def _identity_rows(db, items: list[dict]) -> dict[tuple[str, str, str], dict]:
    """Load exactly the identities in this raised batch, keyed on full identity.

    Keyed on integration/type/external_id, never external_id alone, so an
    ordinary artifact that happens to share an external id is never mistaken for
    a decision. Bounded by the batch: the cost never grows with the number of
    decisions that have ever existed.
    """
    wanted = {(item["identity"]["integration"], item["identity"]["type"],
               item["external_id"]) for item in items}
    if not wanted:
        return {}
    external_ids = sorted({key[2] for key in wanted})
    marks = ",".join("?" for _ in external_ids)
    rows = db.execute(
        f"""SELECT id AS artifact_id, external_id, integration, type, title,
                   metadata, created_at
              FROM artifact_identities
             WHERE state='active' AND external_id IN ({marks})""",
        tuple(external_ids),
    ).fetchall()
    return {_identity_key(row): row for row in rows if _identity_key(row) in wanted}


# --- the queue ---------------------------------------------------------------


def _bodies(identities: dict, items: list[dict]) -> tuple[dict, set[str]]:
    bodies: dict[tuple[str, str, str], dict] = {}
    thread_ids: set[str] = set()
    for item in items:
        key = (item["identity"]["integration"], item["identity"]["type"],
               item["external_id"])
        row = identities.get(key)
        if row is None or row["type"] != decision_projection.DECISION_TYPE:
            continue
        body = decision_projection._body(row)
        if body is not None:
            bodies[key] = body
            thread_ids.add(body["thread_id"])
    return bodies, thread_ids


def annotate(items: list[dict], *, db) -> list[dict]:
    """Attach renderer + detail to already-authorized raised attention rows.

    Callers authorize first; this module never widens access. A row gains a
    renderer ONLY when its payload could be built.
    """
    identities = _identity_rows(db, items)
    bodies, thread_ids = _bodies(identities, items)
    sources = _sources(db, thread_ids)
    answerable = decision_projection.answerable(db, {
        identities[key]["artifact_id"]: body["source_node_id"]
        for key, body in bodies.items()
    })
    deferred = artifact_links.deferred_attention_artifact_ids(
        [row["artifact_id"] for row in identities.values()], db=db,
    )

    annotated = []
    for item in items:
        key = (item["identity"]["integration"], item["identity"]["type"],
               item["external_id"])
        row = identities.get(key)
        body = bodies.get(key)
        detail = None
        if row is not None and body is not None:
            detail = _decision_detail(
                row, body, sources.get(body["thread_id"]),
                answerable.get(row["artifact_id"], False),
            )
        if row is None or detail is None:
            annotated.append({
                **item, "renderer": None, "detail": None,
                "deferred": row is not None and row["artifact_id"] in deferred,
                "priority": 0, "raised_at": "",
            })
            continue
        annotated.append({
            **item, "renderer": DECISION, "detail": detail,
            "deferred": row["artifact_id"] in deferred,
            "priority": body["order_key"],
            "raised_at": body.get("raised_at") or row["created_at"],
        })
    return annotated


def queue(annotated: list[dict]) -> list[dict]:
    """The canonical order the user works through, across every source."""
    typed = [
        row for row in annotated
        if row["renderer"] is not None and not row["deferred"]
    ]
    return sorted(
        typed,
        key=lambda row: (row["priority"], row["raised_at"], row["external_id"]),
    )


def generic(annotated: list[dict]) -> list[dict]:
    """Rows no typed renderer claims and whose Needs-you state is not deferred."""
    return [row for row in annotated if row["renderer"] is None and not row["deferred"]]
