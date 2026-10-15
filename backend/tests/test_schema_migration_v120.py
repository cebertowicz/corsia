"""Schema 119 -> 120 withdraws stale review attention at startup."""
from __future__ import annotations

import sqlite3
import uuid

from infra.database_migrations import MIGRATIONS
from infra.migration_119_to_120_withdraw_stale_review_attention import (
    migrate_119_to_120,
    withdraw_stale_review_attention,
)
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION
from plans import holds, service
from plans.boundary import PlanActor, mutate
from registry import attention, models


OWNER = PlanActor("owner", "user", "project")


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')",
    )
    db.execute(
        """INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
           VALUES ('owner','user','project','test/model','t','t')""",
    )
    db.commit()
    return db


def _artifact(db: sqlite3.Connection, external_id: str, artifact_type: str = "document") -> str:
    models.register_artifact(
        integration="workspace", type=artifact_type, external_id=external_id,
        parent_thread_id="owner", title=external_id, db=db,
    )
    return str(db.execute(
        "SELECT id FROM artifact_identities WHERE external_id=?", (external_id,),
    ).fetchone()["id"])


def _raise_ask(db: sqlite3.Connection, artifact_id: str):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    mutate(
        plan.id, "block_node", {
            "node_id": node.id, "reason_key": "review", "reason": "Review it",
            "owner_kind": "user", "ask_artifact_id": artifact_id,
            "expected_revision": service._node_row(db, node.id)["revision"],
        }, actor=OWNER, provenance="test.stale_attention", mutation_id=uuid.uuid4().hex, db=db,
    )
    hold = db.execute(
        "SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (node.id,),
    ).fetchone()
    return node, str(hold["id"])


def test_registers_the_stale_review_attention_migration():
    assert SCHEMA_VERSION == 120
    assert MIGRATIONS[119] is migrate_119_to_120


def test_withdraws_only_stale_review_attention_and_is_idempotent():
    db = _db()
    try:
        stale = _artifact(db, "stale.md")
        stale_node, stale_hold = _raise_ask(db, stale)
        holds.clear(
            stale_node.id, stale_hold, expected_kind="blocked",
            expected_revision=service._node_row(db, stale_node.id)["revision"],
            actor_id="owner", provenance="test.clear", db=db,
        )
        attention.raise_attention(stale, db=db)

        open_ask = _artifact(db, "open.md")
        _raise_ask(db, open_ask)

        decision = _artifact(db, "decision", artifact_type="plan-decision")
        models.update_metadata(
            "workspace", "plan-decision", "decision", {
                "attention_state": "raised", "plan_decision": {"state": "raised"},
            }, db=db,
        )

        deferred = _artifact(db, "deferred.md")
        models.update_metadata(
            "workspace", "document", "deferred.md", {"attention_state": "deferred"}, db=db,
        )

        migrate_119_to_120(db)
        assert attention.get_attention_state(stale, db=db) == "cleared"
        assert attention.get_attention_state(open_ask, db=db) == "raised"
        assert attention.get_attention_state(decision, db=db) == "raised"
        assert attention.get_attention_state(deferred, db=db) == "deferred"
        assert withdraw_stale_review_attention(db) == 0
    finally:
        db.close()
