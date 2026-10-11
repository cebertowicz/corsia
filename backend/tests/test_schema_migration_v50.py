"""Schema v50 closes the review tasks decision withdrawals failed before #2589."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
import json
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from plans import closure, decision_lifecycle, dependencies, service
from tests.plan_writer_authorizer import raw_plan_seed

_ARCHIVED = "2026-09-20T10:00:00.000000+00:00"


def _node(db, plan_id: str, title: str) -> str:
    return service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} done", db=db,
    ).id


def _fail(db, node_id: str, *, actor_id: str, provenance: str) -> None:
    """Write the failure the way the runtime's transition left it."""
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='failed' WHERE id=?", (node_id,))
    service._event(
        db, node_id, "state_transition", actor_id=actor_id, provenance=provenance,
        prior_state="active", new_state="failed",
    )


def _invalidated(db, node_id: str, *, actor_id: str, provenance: str) -> None:
    service._event(
        db, node_id, "decision_invalidated", actor_id=actor_id,
        provenance=provenance, payload={"reason": "no longer needed"},
    )


def _withdrawn_before_2589(db, node_id: str, **who) -> None:
    """Pre-#2589 withdrawal: fail the review task, then record the withdrawal."""
    _fail(db, node_id, **who)
    _invalidated(db, node_id, **who)


def _legacy_db(path):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        initialize_schema(db)
        db.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
            " VALUES ('owner','user','Owner','t','t')"
        )
        plan = service.ensure_plan("thread", "owner", db=db)
        ids = {key: _node(db, plan.id, key) for key in (
            "invalidated", "owner_archived", "failed_then_withdrawn",
            "withdrawn_archived", "plain_failed",
        )}
        _withdrawn_before_2589(
            db, ids["invalidated"], actor_id="agent", provenance="tool.plans",
        )
        _withdrawn_before_2589(
            db, ids["owner_archived"], actor_id="lifecycle:owner",
            provenance="thread_archive",
        )
        # Failed on its own first; the later withdrawal left it alone, so red is true.
        _fail(db, ids["failed_then_withdrawn"], actor_id="agent", provenance="tool.plans")
        _invalidated(
            db, ids["failed_then_withdrawn"], actor_id="lifecycle:owner",
            provenance="thread_archive",
        )
        _withdrawn_before_2589(
            db, ids["withdrawn_archived"], actor_id="agent", provenance="tool.plans",
        )
        with raw_plan_seed(db):
            db.execute(
                "UPDATE plan_nodes SET archived_at=? WHERE id=?",
                (_ARCHIVED, ids["withdrawn_archived"]),
            )
        _fail(db, ids["plain_failed"], actor_id="agent", provenance="tool.plans")
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=49")
        db.commit()
    return plan.id, ids


def _rows(db) -> dict[str, tuple]:
    return {
        row[0]: row[1:]
        for row in db.execute("SELECT id, state, revision FROM plan_nodes")
    }


def _last_transition(db, node_id: str) -> tuple:
    row = db.execute(
        "SELECT event, new_state, payload_json FROM plan_node_events"
        " WHERE node_id=? AND event='state_transition'"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (node_id,),
    ).fetchone()
    return row[0], row[1], json.loads(row[2])


def _plan_revision(db, plan_id: str) -> int:
    return db.execute("SELECT revision FROM plans WHERE id=?", (plan_id,)).fetchone()[0]


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 109


def test_only_withdrawal_failures_close(tmp_path):
    path = tmp_path / "withdrawn.db"
    plan_id, ids = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        before, plan_before = _rows(db), _plan_revision(db, plan_id)

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 49, SCHEMA_VERSION)
        after = _rows(db)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated

        closed = {"invalidated", "owner_archived"}
        for key, node_id in ids.items():
            state, revision = after[node_id]
            if key in closed:
                assert (state, revision) == ("closed", before[node_id][1] + 1), key
                assert _last_transition(db, node_id) == (
                    "state_transition", "closed",
                    {"reason": decision_lifecycle.WITHDRAWN},
                ), key
            else:
                assert after[node_id] == before[node_id], key
        # A caller holding the old plan revision is refused, not misled.
        assert _plan_revision(db, plan_id) > plan_before


def _holding(db, plan_id: str, title: str) -> str:
    """A review task with a dependent's edge on it and an armed watch."""
    from automation import watches

    node_id = _node(db, plan_id, title)
    dependent = _node(db, plan_id, f"After {title}")
    dependencies.add_dependency(dependent, node_id, db=db)
    watches.arm_watch(
        node_id, {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
        owner_thread="owner", plan_id=plan_id, plan_node_id=node_id,
        armed_by_actor_id="owner", db=db,
    )
    return node_id


def _held(db, node_id: str) -> tuple[int, int]:
    edges = db.execute(
        "SELECT count(*) FROM plan_node_dependencies"
        " WHERE node_id=? OR depends_on_node_id=?", (node_id, node_id),
    ).fetchone()[0]
    armed = db.execute(
        "SELECT count(*) FROM expected_event_watches"
        " WHERE plan_node_id=? AND status='armed'", (node_id,),
    ).fetchone()[0]
    return edges, armed


def test_a_migrated_close_holds_what_a_runtime_withdrawal_holds(tmp_path):
    """The migrated close and today's withdrawal share one release."""
    path = tmp_path / "parity.db"
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        initialize_schema(db)
        db.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
            " VALUES ('owner','user','Owner','t','t')"
        )
        plan = service.ensure_plan("thread", "owner", db=db)
        legacy = _holding(db, plan.id, "Legacy review")
        runtime = _holding(db, plan.id, "Runtime review")
        _withdrawn_before_2589(db, legacy, actor_id="agent", provenance="tool.plans")
        assert _held(db, legacy) == (1, 1)
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=49")
        db.commit()

        migrate_to_current(db, SCHEMA_VERSION)
        closure.close_node(
            runtime, reason=decision_lifecycle.WITHDRAWN, actor_id="agent",
            provenance="tool.plans", db=db,
        )

        assert _held(db, legacy) == _held(db, runtime) == (0, 0)
        assert _last_transition(db, legacy) == _last_transition(db, runtime) == (
            "state_transition", "closed", {"reason": decision_lifecycle.WITHDRAWN},
        )
