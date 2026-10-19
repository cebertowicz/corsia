"""Schema v37 moves dependency edges stranded on rejected submissions (#1730)."""

from __future__ import annotations

from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from tests.schema_rewind import rewind_below_v42, rewind_to_v88_policy
from plans import admission, service
from tests.plan_review_helpers import submit_review_evidence
from tests.test_plan_rejection_revision import (
    _db, _events, _plain_step, _reject, _revision_of, _start,
)
from tests.plan_writer_authorizer import raw_plan_seed


def _edges(db, node_id: str) -> list[str]:
    return [
        str(row[0]) for row in db.execute(
            "SELECT depends_on_node_id FROM plan_node_dependencies WHERE node_id=? "
            "ORDER BY depends_on_node_id",
            (node_id,),
        ).fetchall()
    ]


def _strand(db, node_id: str, on: str) -> None:
    """Seed the pre-v37 shape: the dependent still waits on the rejected submission."""
    db.execute("DELETE FROM plan_node_dependencies WHERE node_id=?", (node_id,))
    db.execute(
        "INSERT INTO plan_node_dependencies (node_id,depends_on_node_id,created_at) "
        "VALUES (?,?,'t')",
        (node_id, on),
    )


def test_stranded_edges_follow_the_rejections_to_the_newest_revision(monkeypatch):
    assert SCHEMA_VERSION == 129
    db = _db(monkeypatch)
    try:
        plan, step = _plain_step(db)
        waiting, settled, doubled = (
            service.add_node(plan.id, kind="task", title=title, acceptance_criteria="Done", db=db)
            for title in ("Ship it", "Old follow-on", "Worked around")
        )
        db.commit()
        # Rejected twice: the step, then its first revision.
        _reject(db, plan.id, step.id)
        first = _revision_of(db, step.id)["id"]
        _start(db, plan.id, first)
        submit_review_evidence(first, db=db, actor_id="parent")
        _reject(db, plan.id, first)
        newest = _revision_of(db, first)["id"]
        # Before v55 a rejection failed its submission rather than closing it.
        with raw_plan_seed(db):
            db.execute(
                "UPDATE plan_nodes SET state='failed' WHERE id IN (?,?)", (step.id, first),
            )
        _strand(db, waiting.id, step.id)
        _strand(db, settled.id, step.id)
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (settled.id,))
        # An agent already added the edge to the newest revision by hand.
        _strand(db, doubled.id, step.id)
        db.execute(
            "INSERT INTO plan_node_dependencies (node_id,depends_on_node_id,created_at) "
            "VALUES (?,?,'t')",
            (doubled.id, newest),
        )
        rewind_below_v42(db)
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("PRAGMA user_version=36")
        db.commit()
        assert [c["reason_key"] for c in admission.project(waiting.id, db=db)["causes"]] == [
            "dependency_failed",
        ]

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 36, SCHEMA_VERSION)
        assert _edges(db, waiting.id) == [newest]
        assert [c["reason_key"] for c in admission.project(waiting.id, db=db)["causes"]] == [
            "dependency",
        ]
        assert _events(db, waiting.id, "dependency_added")[-1] == {"depends_on_node_id": newest}
        # Settled work is never rewritten.
        assert _edges(db, settled.id) == [step.id]
        assert _events(db, settled.id, "dependency_removed") == []
        # A hand-added edge is kept once, never added twice.
        assert _edges(db, doubled.id) == [newest]
        assert {"depends_on_node_id": newest} not in _events(db, doubled.id, "dependency_added")
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
    finally:
        db.close()
