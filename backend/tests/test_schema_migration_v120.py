"""Schema 119 -> 120 withdraws open asks on retired tasks."""
from __future__ import annotations

import json
import sqlite3
import uuid

from infra.database_migrations import MIGRATIONS
from infra.migration_119_to_120_withdraw_retired_user_asks import migrate_119_to_120
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION
from plans import service
from plans.boundary import PlanActor, mutate
from registry import models
from tests.plan_writer_authorizer import raw_plan_seed




def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')",
    )
    for thread_id in ("owner", "gone"):
        db.execute(
            """INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
               VALUES (?,'user','project','test/model','t','t')""",
            (thread_id,),
        )
    db.commit()
    return db


def _artifact(db: sqlite3.Connection, external_id: str, owner: str = "owner") -> str:
    models.register_artifact(
        integration="workspace", type="document", external_id=external_id,
        parent_thread_id=owner, title=external_id, db=db,
    )
    return str(db.execute(
        "SELECT id FROM artifact_identities WHERE external_id=?", (external_id,),
    ).fetchone()["id"])


def _raise_ask(db: sqlite3.Connection, artifact_id: str, owner: str = "owner"):
    plan = service.ensure_plan("thread", owner, db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    mutate(
        plan.id, "block_node", {
            "node_id": node.id, "reason_key": "review", "reason": "Review it",
            "owner_kind": "user", "ask_artifact_id": artifact_id,
            "expected_revision": service._node_row(db, node.id)["revision"],
        }, actor=PlanActor(owner, "user", "project"), provenance="test.migration_120",
        mutation_id=uuid.uuid4().hex, db=db,
    )
    hold = db.execute(
        "SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (node.id,),
    ).fetchone()
    return node, str(hold["id"])


def test_registers_the_hold_only_migration():
    assert SCHEMA_VERSION == 124
    assert MIGRATIONS[119] is migrate_119_to_120
    assert 124 not in MIGRATIONS


def test_withdraws_terminal_and_archived_asks_but_leaves_live_asks_open():
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
            retired.append(hold_id)
        # A deleted owner's plan archived with its task still open (migration 58).
        node, hold_id = _raise_ask(db, _artifact(db, "gone.md", "gone"), "gone")
        with raw_plan_seed(db):
            db.execute("UPDATE plans SET state='archived' WHERE id=?", (node.plan_id,))
        retired.append(hold_id)
        live = _artifact(db, "live.md")
        _, live_hold = _raise_ask(db, live)
        before_metadata = db.execute(
            "SELECT metadata FROM artifact_identities WHERE id=?", (live,),
        ).fetchone()["metadata"]
        db.commit()

        migrate_119_to_120(db)

        for hold_id in retired:
            row = db.execute(
                "SELECT cleared_at,clearance_event_id FROM plan_node_holds WHERE id=?", (hold_id,),
            ).fetchone()
            assert row["cleared_at"] is not None
            payload = json.loads(db.execute(
                "SELECT payload_json FROM plan_node_events WHERE id=?", (row["clearance_event_id"],),
            ).fetchone()["payload_json"])
            assert payload["detail"] == {"ended": "closed"}
        assert db.execute(
            "SELECT cleared_at FROM plan_node_holds WHERE id=?", (live_hold,),
        ).fetchone()["cleared_at"] is None
        assert db.execute(
            "SELECT metadata FROM artifact_identities WHERE id=?", (live,),
        ).fetchone()["metadata"] == before_metadata
    finally:
        db.close()


def test_is_idempotent_and_never_writes_attention_metadata():
    db = _db()
    try:
        artifact_id = _artifact(db, "retired.md")
        node, hold_id = _raise_ask(db, artifact_id)
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (node.id,))
        before_metadata = db.execute(
            "SELECT metadata FROM artifact_identities WHERE id=?", (artifact_id,),
        ).fetchone()["metadata"]
        db.commit()

        migrate_119_to_120(db)
        first_event_count = db.execute(
            "SELECT count(*) FROM plan_node_events WHERE node_id=? AND event='hold_cleared'", (node.id,),
        ).fetchone()[0]
        migrate_119_to_120(db)

        assert first_event_count == 1
        assert db.execute(
            "SELECT count(*) FROM plan_node_events WHERE node_id=? AND event='hold_cleared'", (node.id,),
        ).fetchone()[0] == first_event_count
        assert db.execute(
            "SELECT metadata FROM artifact_identities WHERE id=?", (artifact_id,),
        ).fetchone()["metadata"] == before_metadata
        assert db.execute(
            "SELECT cleared_at FROM plan_node_holds WHERE id=?", (hold_id,),
        ).fetchone()["cleared_at"] is not None
    finally:
        db.close()


def test_copies_only_a_uniquely_open_deferred_ask_to_its_hold():
    db = _db()
    try:
        artifact_id = _artifact(db, "deferred.md")
        _, hold_id = _raise_ask(db, artifact_id)
        db.execute(
            "UPDATE artifact_identities SET metadata=? WHERE id=?",
            (json.dumps({"attention_state": "deferred"}), artifact_id),
        )
        db.commit()

        migrate_119_to_120(db)
        assert db.execute(
            "SELECT hold_id FROM plan_ask_deferrals WHERE hold_id=?", (hold_id,),
        ).fetchone()["hold_id"] == hold_id

        migrate_119_to_120(db)
        assert db.execute(
            "SELECT count(*) FROM plan_ask_deferrals WHERE hold_id=?", (hold_id,),
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_leaves_a_deferred_artifact_with_multiple_open_asks_untouched():
    from plans import holds, user_ask

    db = _db()
    try:
        artifact_id = _artifact(db, "multiple-deferred.md")
        first, _ = _raise_ask(db, artifact_id)
        second = service.add_node(
            service._node_row(db, first.id)["plan_id"], kind="task", title="Second review",
            acceptance_criteria="Reviewed", db=db,
        )
        # A duplicate from before the one-open-request rule, seeded below it.
        holds.place_hold(
            second.id, kind="blocked", reason_key="review", reason="Review it", owner_kind="user",
            owner_id=artifact_id, expected_revision=service._node_row(db, second.id)["revision"],
            actor_id="owner", provenance="test.migration_120", db=db,
        )
        user_ask.attach(
            db, second.id, artifact_id, actor_id="owner", provenance="test.migration_120",
        )
        before = json.dumps({"attention_state": "deferred", "other": "keep"})
        db.execute("UPDATE artifact_identities SET metadata=? WHERE id=?", (before, artifact_id))
        db.commit()

        migrate_119_to_120(db)

        assert db.execute(
            "SELECT metadata FROM artifact_identities WHERE id=?", (artifact_id,),
        ).fetchone()["metadata"] == before
        assert db.execute("SELECT count(*) FROM plan_ask_deferrals").fetchone()[0] == 0
    finally:
        db.close()
