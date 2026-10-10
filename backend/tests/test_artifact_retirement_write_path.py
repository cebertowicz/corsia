"""Node settlement against artifacts produced by the real file write path.

The retirement exemption keys on ``retention`` and the ``zone`` metadata that
``register_write_strict`` derives from the path. Tests that hand-set those two
fields would still pass if the write path stopped producing them, so this
contract registers actual files and lets the path decide.
"""

from __future__ import annotations

import pytest

from infra import config
from infra.database import get_db
from infra.workspace_paths import WorkspaceSelection
from plans import service
from registry import service as registry_service
from registry.registration_writer import RegistrationOrigin
from tests.plan_review_helpers import submit_review_evidence
from tests.registry_helpers import make_session


@pytest.fixture()
def write_path_scope(isolated_db, tmp_path, monkeypatch):
    """A real workspace root whose files register through the normal path."""
    workspace = tmp_path / "workspace"
    (workspace / "shared" / "reference").mkdir(parents=True)
    (workspace / ".threads" / "t1" / "working").mkdir(parents=True)
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(
        config,
        "WORKSPACE_SELECTION",
        WorkspaceSelection(
            workspace_id="workspace-stable-id",
            root=workspace.resolve(),
            name="Test workspace",
        ),
    )
    monkeypatch.setattr(
        registry_service,
        "get_allowed_dirs_rw",
        lambda **_kwargs: [workspace.resolve()],
    )
    db = get_db()
    return workspace, make_session(db, prefix="write-path"), db


def _active_node_with_turn(db, thread_id: str, turn_id: str):
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title="Produce artifact",
        acceptance_criteria="Artifact is produced and verified",
        db=db,
    )
    node = service.transition_node(node.id, "active", db=db)
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
            plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role)
           VALUES (?,?,0,'native/test','running','t0','boot',?,?,?,?,?)""",
        (
            turn_id, thread_id, plan.id, node.id,
            f"attempt-{turn_id}", node.commitment_id, "execution",
        ),
    )
    db.commit()
    return node


def _accept(db, node_id: str) -> None:
    service.transition_node(node_id, "review_requested", db=db)
    submit_review_evidence(node_id, db=db)
    service.transition_node(
        node_id,
        "accepted",
        verification_result="passed",
        verified_by="reviewer",
        db=db,
    )


def _registration(db, artifact_id: str, thread_id: str):
    return db.execute(
        """SELECT * FROM artifact_registrations
           WHERE artifact_id=? AND parent_thread_id=?""",
        (artifact_id, thread_id),
    ).fetchone()


def test_written_shared_document_survives_settlement_and_scratch_does_not(
    write_path_scope,
):
    workspace, thread_id, db = write_path_scope
    node = _active_node_with_turn(db, thread_id, "turn-write")
    origin = RegistrationOrigin(thread_id, "turn-write", "write_file")

    tracker = workspace / "shared" / "reference" / "TRACKER.md"
    tracker.write_text("long-lived tracking document\n")
    scratch = workspace / ".threads" / "t1" / "working" / "scratch.md"
    scratch.write_text("task-scoped notes\n")

    kept = registry_service.register_write_strict(
        thread_id, str(tracker), origin=origin, db=db,
    )
    retired = registry_service.register_write_strict(
        thread_id, str(scratch), origin=origin, db=db,
    )

    # The path — not the caller — decides both exemption inputs.
    assert kept["retention"] == "durable"
    assert retired["retention"] == "ephemeral"

    _accept(db, node.id)

    kept_row = _registration(db, kept["id"], thread_id)
    assert kept_row["surfacing"] == "visible"
    assert kept_row["retired_at"] is None

    retired_row = _registration(db, retired["id"], thread_id)
    assert retired_row["surfacing"] == "settled"
    assert retired_row["retired_at"] is not None
