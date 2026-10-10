"""What plan_update accepts and what it reports back (R1 slice B, #1813).

These drive the real mutation boundary: either verb makes work current, a
stale revision says which revision the row holds now, a reorder reports the
siblings it renumbered, an acceptance keeps the reviewer's note, and evidence
attaches the results the submitting thread owns.
"""

from __future__ import annotations

import pytest

from plans import review_evidence, service
from plans.boundary import PlanActor, mutate
from tests.plan_scene import (  # noqa: F401 - shared plan fixtures
    ACTOR,
    _artifact,
    _mid,
    db,
    raise_decision,
    scene,
)


def _revision(db, node_id: str) -> int:
    return service.get_node(node_id, db=db).revision


def _mutate(db, plan_id, operation, params):
    return mutate(
        plan_id, operation, params, actor=ACTOR, provenance="test",
        mutation_id=_mid(), db=db,
    ).result


def _accepted_scene(db, scene, *, evidence: str = "Focused checks passed."):
    """Leave the scene's own task review-requested by its owner."""
    _mutate(db, scene.plan_id, "request_review", {
        "node_id": scene.own, "expected_revision": _revision(db, scene.own),
        "evidence": evidence,
    })
    return _revision(db, scene.own)


@pytest.mark.parametrize("operation", ["start", "activate"])
def test_either_verb_makes_pending_work_current(db, scene, operation):
    result = _mutate(db, scene.plan_id, operation, {
        "node_id": scene.pending, "expected_revision": _revision(db, scene.pending),
    })

    assert result["node"]["state"] == "active"
    assert result["admission"]["selection"] is not None


@pytest.mark.parametrize("operation", ["start", "activate"])
def test_either_verb_selects_an_active_task(db, scene, operation):
    result = _mutate(db, scene.plan_id, operation, {
        "node_id": scene.own, "expected_revision": _revision(db, scene.own),
    })

    assert result["node"]["state"] == "active"
    assert result["admission"]["selection"] is not None


def test_settled_work_cannot_become_current_and_the_refusal_names_its_state(db, scene):
    service.transition_node(scene.own, "failed", db=db)
    db.commit()

    with pytest.raises(service.PlanError) as refusal:
        _mutate(db, scene.plan_id, "start", {
            "node_id": scene.own, "expected_revision": _revision(db, scene.own),
        })

    assert "is failed and cannot reopen" in str(refusal.value)
    assert "add_follow_up" in str(refusal.value)


def test_a_stale_node_revision_states_the_revision_the_row_holds(db, scene):
    current = _revision(db, scene.own)

    with pytest.raises(service.PlanError) as refusal:
        _mutate(db, scene.plan_id, "edit", {
            "node_id": scene.own, "expected_revision": current - 1,
            "title": "Renamed",
        })

    assert str(refusal.value) == (
        f"plan node revision conflict: expected {current - 1}, "
        f"current {current} (node {scene.own})"
    )


def test_a_stale_plan_revision_states_the_revision_the_plan_holds(db, scene):
    current = service._plan_row(db, scene.plan_id)["revision"]

    with pytest.raises(service.PlanError) as refusal:
        mutate(  # as the user: an agent's unprompted pause is refused first (#1969)
            scene.plan_id, "pause", {"expected_revision": current + 3},
            actor=PlanActor(None, "user-1", None, kind="user"), provenance="test",
            mutation_id=_mid(), db=db,
        )

    assert str(refusal.value) == (
        f"plan revision conflict: expected {current + 3}, "
        f"current {current} (plan {scene.plan_id})"
    )


def test_a_stale_decision_revision_states_the_current_revision_only(db):
    from plans import decisions

    decision = raise_decision(db, "root", "root")

    with pytest.raises(service.PlanError) as refusal:
        decisions.invalidate(
            decision["id"], expected_revision=0, mutation_id=_mid(),
            actor=ACTOR, provenance="test", db=db,
        )

    assert str(refusal.value) == (
        f"plan decision revision conflict: expected 0, "
        f"current {decision['revision']}"
    )
    assert decision["id"] not in str(refusal.value)


def test_reorder_leaves_every_sibling_revision_alone(db, scene):
    """Only the moved node changed, so a sibling's pending write still applies."""
    third = service.add_node(
        scene.plan_id, kind="task", title="Third", acceptance_criteria="Third DOD",
        db=db,
    )
    db.commit()
    before = {node: _revision(db, node) for node in (scene.pending, third.id)}
    own_before = _revision(db, scene.own)

    result = _mutate(db, scene.plan_id, "reorder", {
        "node_id": scene.own, "order_key": 0,
        "expected_revision": own_before,
    })

    assert result["node"]["order_key"] == 0
    assert "siblings" not in result
    assert _revision(db, scene.own) == own_before + 1
    assert {node: _revision(db, node) for node in before} == before


def test_acceptance_evidence_is_appended_to_the_reviewed_evidence(db, scene):
    revision = _accepted_scene(db, scene)

    _mutate(db, scene.plan_id, "accept", {
        "node_id": scene.own, "expected_revision": revision,
        "verification_result": "passed", "evidence": "Reviewed the diff.",
    })

    node = service.get_node(scene.own, db=db)
    recorded = review_evidence.latest(
        scene.own, requirements_revision=node.requirements_revision, db=db,
    )
    assert recorded["evidence"] == "Focused checks passed.\n\nAccepted: Reviewed the diff."
    assert node.state == "accepted"


def test_accept_with_follow_up_records_the_verdict_too(db, scene):
    revision = _accepted_scene(db, scene)

    _mutate(db, scene.plan_id, "accept_with_follow_up", {
        "node_id": scene.own, "expected_revision": revision,
        "verification_result": "passed", "title": "Tidy the helper",
        "acceptance_criteria": "The helper is tidy",
        "evidence": "Shipped; one nit follows.",
    })

    node = service.get_node(scene.own, db=db)
    recorded = review_evidence.latest(
        scene.own, requirements_revision=node.requirements_revision, db=db,
    )
    assert recorded["evidence"].endswith("Accepted: Shipped; one nit follows.")


def test_acceptance_without_evidence_leaves_the_submitted_evidence_alone(db, scene):
    revision = _accepted_scene(db, scene)

    _mutate(db, scene.plan_id, "accept", {
        "node_id": scene.own, "expected_revision": revision,
        "verification_result": "passed",
    })

    node = service.get_node(scene.own, db=db)
    recorded = review_evidence.latest(
        scene.own, requirements_revision=node.requirements_revision, db=db,
    )
    assert recorded["evidence"] == "Focused checks passed."


def test_review_evidence_links_the_results_this_thread_owns(db, scene):
    artifact_id = _artifact(db, external_id="owner/repo#41", owner="root")

    _mutate(db, scene.plan_id, "request_review", {
        "node_id": scene.own, "expected_revision": _revision(db, scene.own),
        "evidence": "The PR is open.", "artifact_ids": [artifact_id],
    })

    linked = db.execute(
        """SELECT role FROM plan_node_artifact_links
           WHERE node_id=? AND artifact_id=?""",
        (scene.own, artifact_id),
    ).fetchone()
    assert linked["role"] == "result"
    node = service.get_node(scene.own, db=db)
    assert node.state == "review_requested"


def test_review_evidence_refuses_artifacts_another_thread_owns_by_name(db, scene):
    db.execute(
        "INSERT OR IGNORE INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('stranger','user-1','test/model','t','t')"
    )
    db.commit()
    artifact_id = _artifact(db, external_id="owner/repo#42", owner="stranger")

    with pytest.raises(service.PlanInputError) as refusal:
        _mutate(db, scene.plan_id, "request_review", {
            "node_id": scene.own, "expected_revision": _revision(db, scene.own),
            "evidence": "The PR is open.", "artifact_ids": [artifact_id],
        })

    assert artifact_id in str(refusal.value)
    assert service.get_node(scene.own, db=db).state == "active"


def test_attach_spawned_thread_does_not_accept_spawn_kind(db, scene):
    from plans import boundary_fields

    assert "spawn_kind" not in boundary_fields.OPERATION_FIELDS["attach_spawned_thread"]

    with pytest.raises(service.PlanInputError) as refusal:
        _mutate(db, scene.plan_id, "attach_spawned_thread", {
            "node_id": scene.own, "expected_revision": _revision(db, scene.own),
            "spawned_thread_id": "child", "spawn_kind": "subagent",
        })

    assert "unexpected parameters for attach_spawned_thread: spawn_kind" in str(
        refusal.value
    )
