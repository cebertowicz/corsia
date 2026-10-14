"""Evidence submission for structured-plan review without a second result store."""

from __future__ import annotations

import json
from typing import Any

from infra.packed_rows import fetch_all
from plans import errors, service


def _artifact_ids(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise service.PlanInputError("artifact_ids must be an array")
    normalized = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise service.PlanInputError("artifact_ids must contain nonempty ids")
        artifact_id = item.strip()
        if artifact_id not in normalized:
            normalized.append(artifact_id)
    return normalized



def stop_reason(db, node, *, paused_plan_binds: bool = True) -> str | None:
    """The stop in force on this work, or None when review may proceed.

    Requesting review is a state transition like any other, so a paused plan
    or a live hold must block it. Without this, `request_review` would settle
    work the user has explicitly stopped, moving the node past its recorded
    pause or blocker instead of leaving that stop binding. Acceptance already
    refuses under exactly these conditions. A caller that has already ended
    the node's holds may set ``paused_plan_binds`` False, which skips only the
    paused-plan refusal; a live hold still refuses.

    A system writer settling review for many items in one pass asks this
    first and leaves stopped work open, so one stopped item never raises
    through the pass (2026-10-14: a paused reviewer stalled every wake).
    """
    plan = service._plan_row(db, node["plan_id"])
    if paused_plan_binds and plan["state"] == "paused":
        return "review requires an active plan"
    hold = db.execute(
        """SELECT kind FROM plan_node_holds
           WHERE node_id=? AND cleared_at IS NULL
           ORDER BY CASE kind WHEN 'blocked' THEN 0 ELSE 1 END LIMIT 1""",
        (node["id"],),
    ).fetchone()
    if hold is None:
        return None
    if hold["kind"] == "blocked":
        return "review has unresolved blocking findings"
    return "review has an unresolved pause hold"


def _require_no_stop(db, node, *, paused_plan_binds: bool = True) -> None:
    """Refuse review while a stop is in force on this work (``stop_reason``)."""
    reason = stop_reason(db, node, paused_plan_binds=paused_plan_binds)
    if reason is not None:
        raise service.PlanError(reason)


def _bind_artifacts(
    db, node_id: str, ids: list[str], *, owner_thread_id: str | None,
    actor_id: str | None, provenance: str,
) -> None:
    """Link the results this evidence cites, refusing only artifacts to link.

    Naming an artifact in review evidence *is* the intent to attach it, so an
    artifact this thread owns is linked here rather than refused with a second
    call the caller must discover. An artifact the thread does not own is not
    this thread's to attach: those are refused by name, so the caller sees
    which ones, rather than a count or a generic rule.
    """
    from plans import artifact_links

    unowned = []
    for artifact_id in ids:
        if _linked_result(db, node_id, artifact_id):
            continue
        _known, owner = artifact_links.identity_owner(artifact_id, db=db)
        if owner is not None and owner == owner_thread_id:
            artifact_links.link(
                node_id, artifact_id, "result", actor_id=actor_id,
                provenance=provenance, db=db,
            )
            continue
        unowned.append(artifact_id)
    if unowned:
        raise service.PlanInputError(
            "artifact_ids must name active results this thread owns; "
            f"not owned: {', '.join(unowned)}"
        )


def _linked_result(db, node_id: str, artifact_id: str) -> bool:
    """Whether one active artifact is already this node's recorded result."""
    return db.execute(
        """SELECT 1 FROM plan_node_artifact_links l
           JOIN artifact_identities a ON a.id=l.artifact_id
           WHERE l.node_id=? AND l.artifact_id=? AND l.role='result'
             AND a.state='active'""",
        (node_id, artifact_id),
    ).fetchone() is not None


def record_verdict(
    node_id: str, evidence: str | None, *, verdict: str, actor_id: str | None,
    provenance: str, db,
) -> None:
    """Keep the settler's own evidence with the node, before its terminal transition.

    A verdict (``Accepted`` or ``Failed``) is a judgement, and its reason
    belongs with the evidence it judged, not in a separate store. With current
    evidence already submitted, the note is appended to it. Without any -- a
    delegated step the harvest put up for review (#1977), or work that failed
    before it was ever submitted (#2018) -- the settler's evidence is the
    evidence: acceptance reads it as the verification of the current
    requirements, and a failed node carries why it failed.
    """
    if evidence is None:
        return
    evidence = service._required_text(evidence, "evidence")
    node = service._node_row(db, node_id)
    if node["state"] in service._TERMINAL_STATES:
        return  # A repeated settlement observes the verdict already recorded.
    prior = latest(
        node_id, requirements_revision=node["requirements_revision"], db=db,
    )
    text = evidence if prior is None else f"{prior['evidence']}\n\n{verdict}: {evidence}"
    service._event(
        db, node_id, "review_evidence_submitted", actor_id=actor_id,
        provenance=provenance,
        payload={
            "evidence": text,
            "artifact_ids": [] if prior is None else list(prior["artifact_ids"]),
            "requirements_revision": node["requirements_revision"],
        },
    )


def submit(
    node_id: str,
    *,
    evidence: str | None,
    artifact_ids: object = None,
    expected_revision: int | None,
    actor_id: str | None,
    provenance: str,
    db,
    owner_thread_id: str | None = None,
    paused_plan_binds: bool = True,
):
    """Append review evidence and move active work to review exactly once.

    ``paused_plan_binds`` is False only for Mark done, which has already ended
    the task's own holds: it skips the paused-plan refusal, because a plan the
    user paused does not stop the user settling their own task.
    """

    node = service._live_node_row(db, node_id)
    if expected_revision is not None:
        errors.require_node_revision(node, expected_revision)
    if node["state"] not in {"active", "review_requested"}:
        raise service.PlanError("review evidence requires active or review-requested work")
    _require_no_stop(db, node, paused_plan_binds=paused_plan_binds)
    evidence = service._required_text(evidence or "", "review evidence")
    ids = _artifact_ids(artifact_ids)
    _bind_artifacts(
        db, node_id, ids, owner_thread_id=owner_thread_id,
        actor_id=actor_id, provenance=provenance,
    )
    service._event(
        db,
        node_id,
        "review_evidence_submitted",
        actor_id=actor_id,
        provenance=provenance,
        payload={
            "evidence": evidence,
            "artifact_ids": ids,
            "requirements_revision": node["requirements_revision"],
        },
    )
    if node["state"] == "active":
        return service._transition(
            db,
            node_id,
            "review_requested",
            actor_id=actor_id,
            provenance=provenance,
            verification_result=None,
            verified_by=None,
        )
    service.touch_node(db, node_id)
    return service._node_from_row(db, service._node_row(db, node_id))


_LATEST = """SELECT payload_json FROM plan_node_events
               WHERE node_id={node} AND event='review_evidence_submitted'
               ORDER BY created_at DESC,rowid DESC LIMIT 1"""


def _current(payload_json: str | None, requirements_revision: int) -> dict[str, Any] | None:
    """Return a well-formed payload for these requirements, else None."""

    try:
        payload = json.loads(payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("evidence"), str)
        or not payload["evidence"].strip()
        or payload.get("requirements_revision") != requirements_revision
        or not isinstance(payload.get("artifact_ids"), list)
    ):
        return None
    return payload


def latest(node_id: str, *, requirements_revision: int, db) -> dict[str, Any] | None:
    """Return current-requirements review evidence or None when absent/malformed."""

    row = db.execute(_LATEST.format(node="?"), (node_id,)).fetchone()
    return None if row is None else _current(row["payload_json"], requirements_revision)


def latest_by_node(
    plan_ids: list[str], *, db, node_ids: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Apply ``latest`` to every node of these plans (or of ``node_ids`` alone) in one read.

    The event trail is append-only, so each node's latest submission is found
    per node in SQL: the read returns one row per node however often its
    evidence was resubmitted.
    """

    if not plan_ids:
        return {}
    from plans.operator_node_facts import id_chunks, scope

    rows = []
    for chunk in id_chunks(node_ids):
        where, params = scope("plan_id", "id", plan_ids, chunk)
        rows.extend(fetch_all(
            db,
            f"""SELECT id,requirements_revision,
                       ({_LATEST.format(node="plan_nodes.id")}) AS payload_json
                FROM plan_nodes WHERE {where}""",
            params,
        ))
    found = {}
    for row in rows:
        payload = _current(row["payload_json"], row["requirements_revision"])
        if payload is not None:
            found[str(row["id"])] = payload
    return found
