"""Schema v39 ends the watches nothing could end (#1707)."""

from __future__ import annotations

import json

from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from tests.schema_rewind import rewind_below_v42, rewind_to_v88_policy
from plans import admission
from tests.test_continuation_watch_expiry import _arm, _db, _expiry_trigger, _plan


def _legacy(db, watch_id: str) -> None:
    """Seed the pre-v39 shape: an event wait armed on no artifact."""
    db.execute(
        "UPDATE expected_event_watches SET condition_type='event', "
        "condition_value='external.done' WHERE id=?",
        (watch_id,),
    )


def _watch(db, watch_id: str):
    return db.execute(
        "SELECT * FROM expected_event_watches WHERE id=?", (watch_id,)
    ).fetchone()


def test_an_unbound_plan_wait_expires_and_wakes_its_owner():
    assert SCHEMA_VERSION == 130
    db = _db()
    try:
        plan, node, _other = _plan(db)
        stuck = _arm(db, plan, node)["watch"]["id"]
        _legacy(db, stuck)
        db.execute(
            """INSERT INTO expected_event_watches
               (id, artifact_integration, artifact_type, artifact_external_id,
                owner_thread_id, expected_event, condition_type, condition_value,
                status, armed_at, created_at, updated_at)
               VALUES ('bound','github','pull_request','acme/repo#1','owner',
                       'ci.complete','event','ci.complete','armed','t','t','t')"""
        )
        rewind_below_v42(db)
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("PRAGMA user_version=38")
        db.commit()

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 38, SCHEMA_VERSION)
        assert _watch(db, stuck)["condition_type"] == "deadline"
        # A watch its artifact's event can still end keeps waiting for it.
        bound = _watch(db, "bound")
        assert (bound["condition_type"], bound["condition_value"]) == ("event", "ci.complete")
        # The node's history names the wait the deadline replaced.
        rearmed = db.execute(
            "SELECT provenance, payload_json FROM plan_node_events "
            "WHERE node_id=? AND event='watch_armed' ORDER BY created_at DESC, id DESC",
            (node.id,),
        ).fetchone()
        assert rearmed["provenance"] == "migration.38_to_39"
        assert json.loads(rearmed["payload_json"])["replaces"] == {
            "type": "event", "value": "external.done",
        }

        # The ordinary expiry sweep takes it on its next pass and hands the
        # owner its task back.
        source, trigger = _expiry_trigger(db)
        assert trigger.subject_id == stuck
        source.settle(trigger, "delivered", "watch_expired", db=db)
        assert _watch(db, stuck)["status"] == "expired"
        projected = admission.project(node.id, db=db)
        assert projected["condition"] == "running"
        assert projected["selection"]["executor_thread_id"] == "owner"
    finally:
        db.close()
