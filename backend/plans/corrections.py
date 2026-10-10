"""Atomic correction creation for invalidated terminal work."""

from __future__ import annotations

from dataclasses import asdict

from plans import dependencies, errors, followups, node_states, service

_OPEN_STATES = frozenset({"pending", "active", "review_requested"})


def _affected_rows(value: object, *, source, db) -> list:
    """Validate exact affected-node revision pairs before writing anything."""

    if not isinstance(value, list) or not value:
        raise service.PlanInputError(
            "affected_nodes must be a nonempty array of node/revision pairs"
        )
    rows = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "node_id",
            "expected_revision",
        }:
            raise service.PlanInputError(
                "affected_nodes entries require only node_id and expected_revision"
            )
        node_id = item["node_id"]
        revision = item["expected_revision"]
        if (
            not isinstance(node_id, str)
            or not node_id.strip()
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise service.PlanInputError(
                "affected_nodes entries require a node id and non-negative revision"
            )
        node_id = node_id.strip()
        if node_id in seen:
            raise service.PlanInputError("affected_nodes must not contain duplicates")
        seen.add(node_id)
        row = service._node_row(db, node_id)
        if (
            row["plan_id"] != source["plan_id"]
            or node_states.retired(row)
        ):
            raise service.PlanError(
                "affected correction work must be live in the source plan"
            )
        if row["state"] not in _OPEN_STATES:
            raise service.PlanError(
                "corrections may block only unfinished affected work"
            )
        errors.require_node_revision(row, revision)
        rows.append(row)
    return rows


def add(
    node_id: str,
    *,
    title: str,
    acceptance_criteria: str,
    affected_nodes: object,
    expected_revision: int,
    actor_id: str | None,
    provenance: str,
    db,
) -> dict:
    """Create required rework while preserving the terminal source as history."""

    source = service._live_node_row(db, node_id)
    errors.require_node_revision(source, expected_revision)
    if source["state"] not in service._TERMINAL_STATES:
        raise service.PlanError("a correction requires terminal source work")
    affected = _affected_rows(affected_nodes, source=source, db=db)
    correction = followups.add_follow_up(
        node_id,
        title=title,
        acceptance_criteria=acceptance_criteria,
        actor_id=actor_id,
        provenance=provenance,
        expected_revision=expected_revision,
        inherit_commitment=True,
        db=db,
    )
    service._event(
        db,
        correction.id,
        "correction_created",
        actor_id=actor_id,
        provenance=provenance,
        payload={
            "source_node_id": node_id,
            "affected_node_ids": [row["id"] for row in affected],
            "commitment_id": source["commitment_id"],
        },
    )
    for row in affected:
        dependencies.add_dependency(
            row["id"],
            correction.id,
            expected_revision=row["revision"],
            actor_id=actor_id,
            provenance=provenance,
            db=db,
        )
    from plans import admission

    return {
        "node": asdict(service.get_node(node_id, db=db)),
        "admission": admission.project(node_id, db=db),
        "correction": asdict(service.get_node(correction.id, db=db)),
        "affected_nodes": [
            asdict(service.get_node(row["id"], db=db)) for row in affected
        ],
    }
