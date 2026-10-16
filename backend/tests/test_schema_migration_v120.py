"""Schema 119 -> 120 withdraws stale review attention at startup."""
from __future__ import annotations

import json
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
from tests.plan_writer_authorizer import raw_plan_seed


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


def test_repairs_open_asks_on_retired_nodes_without_touching_live_ones():
    db = _db()
    try:
        retired = []
        for state, external_id in (("failed", "failed.md"), ("closed", "closed.md"), ("archived", "archived.md")):
            artifact_id = _artifact(db, external_id)
            node, hold_id = _raise_ask(db, artifact_id)
            with raw_plan_seed(db):
                if state == "archived":
                    db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (node.id,))
                else:
                    db.execute("UPDATE plan_nodes SET state=? WHERE id=?", (state, node.id))
            retired.append((artifact_id, hold_id))
        live = _artifact(db, "live.md")
        live_node, live_hold = _raise_ask(db, live)
        db.commit()

        migrate_119_to_120(db)

        for artifact_id, hold_id in retired:
            row = db.execute(
                "SELECT cleared_at,clearance_event_id FROM plan_node_holds WHERE id=?", (hold_id,),
            ).fetchone()
            payload = db.execute(
                "SELECT payload_json FROM plan_node_events WHERE id=?", (row["clearance_event_id"],),
            ).fetchone()["payload_json"]
            assert row["cleared_at"] is not None
            assert json.loads(payload)["detail"] == {"ended": "withdrawn"}
            assert attention.get_attention_state(artifact_id, db=db) == "cleared"
        assert db.execute(
            "SELECT cleared_at FROM plan_node_holds WHERE id=?", (live_hold,),
        ).fetchone()["cleared_at"] is None
        assert attention.get_attention_state(live, db=db) == "raised"

        events_before = db.execute("SELECT COUNT(*) FROM plan_node_events").fetchone()[0]
        migrate_119_to_120(db)
        assert db.execute("SELECT COUNT(*) FROM plan_node_events").fetchone()[0] == events_before

        replacement = service.add_node(
            live_node.plan_id, kind="task", title="Ask again", acceptance_criteria="Reviewed", db=db,
        )
        mutate(
            live_node.plan_id, "block_node", {
                "node_id": replacement.id, "reason_key": "review", "reason": "Review it",
                "owner_kind": "user", "ask_artifact_id": retired[0][0],
                "expected_revision": service._node_row(db, replacement.id)["revision"],
            }, actor=OWNER, provenance="test.stale_attention", mutation_id=uuid.uuid4().hex, db=db,
        )
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (replacement.id,),
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_repairs_each_candidate_through_the_runtime_reconciler(monkeypatch):
    import plans.public

    db = _db()
    try:
        # Its ask ended, but attention stayed raised: nothing raises it now.
        stale = _artifact(db, "stale.md")
        node, hold = _raise_ask(db, stale)
        holds.clear(
            node.id, hold, expected_kind="blocked",
            expected_revision=service._node_row(db, node.id)["revision"],
            actor_id="owner", provenance="test.clear", db=db,
        )
        attention.raise_attention(stale, db=db)
        # Its first ask ended, and a later task asks about it again.
        asked_again = _artifact(db, "asked-again.md")
        node, hold = _raise_ask(db, asked_again)
        holds.clear(
            node.id, hold, expected_kind="blocked",
            expected_revision=service._node_row(db, node.id)["revision"],
            actor_id="owner", provenance="test.clear", db=db,
        )
        _raise_ask(db, asked_again)
        reconciled = []
        reconcile = plans.public.sync_ask_attention

        def recorded(conn, artifact_id, *, outcome):
            reconciled.append((artifact_id, outcome))
            return reconcile(conn, artifact_id, outcome=outcome)

        monkeypatch.setattr(plans.public, "sync_ask_attention", recorded)

        assert withdraw_stale_review_attention(db) == 1
        assert sorted(reconciled) == sorted([(stale, "withdraw"), (asked_again, "withdraw")])
        assert attention.get_attention_state(stale, db=db) == "cleared"
        assert json.loads(db.execute(
            "SELECT metadata FROM artifact_identities WHERE id=?", (stale,),
        ).fetchone()["metadata"])["attention_outcome"] == "withdraw"
        assert attention.get_attention_state(asked_again, db=db) == "raised"
    finally:
        db.close()


def test_keeps_attention_no_ended_ask_left_behind():
    db = _db()
    try:
        # A parent review approved this to the user after the child's ask ended.
        approved = _artifact(db, "approved.md")
        node, hold = _raise_ask(db, approved)
        holds.clear(
            node.id, hold, expected_kind="blocked",
            expected_revision=service._node_row(db, node.id)["revision"],
            actor_id="owner", provenance="test.clear", db=db,
        )
        models.update_metadata(
            "workspace", "document", "approved.md",
            {"attention_state": "raised", "disposition_outcome": "approve_to_user"}, db=db,
        )
        # A parent review's deferral that its watch raised again.
        reraised = _artifact(db, "reraised.md")
        models.update_metadata(
            "workspace", "document", "reraised.md",
            {"attention_state": "raised", "disposition_outcome": "defer_until"}, db=db,
        )
        # Raised with no ask ever linked: no evidence it is stale.
        unlinked = _artifact(db, "unlinked.md")
        attention.raise_attention(unlinked, db=db)

        assert withdraw_stale_review_attention(db) == 0
        for artifact_id in (approved, reraised, unlinked):
            assert attention.get_attention_state(artifact_id, db=db) == "raised"
    finally:
        db.close()
