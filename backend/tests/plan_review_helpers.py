"""Shared test-only structured-plan review evidence helper."""

from __future__ import annotations

from plans import mutations, service


def submit_review_evidence(
    node_id: str,
    *,
    db,
    evidence: str = "Focused test proof passed",
    actor_id: str = "test-reviewer",
):
    """Submit evidence against the node's exact current requirements revision."""

    current = service.get_node(node_id, db=db)
    return mutations.request_review(
        node_id,
        evidence=evidence,
        expected_revision=current.revision,
        actor_id=actor_id,
        provenance="test.review_evidence",
        db=db,
    )
