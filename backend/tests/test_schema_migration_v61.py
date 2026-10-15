"""Schema v61 settles worker steps left open under a finished delegated step."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from plans import mutations, service, worker_retirement
from tests.plan_writer_authorizer import raw_plan_seed

_PROVENANCE = "migration.60_to_61"


def _thread(db, thread_id: str, *, spawned_by: str | None = None) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,title,spawned_by,created_at,updated_at)"
        " VALUES (?,'user','T',?,'t','t')",
        (thread_id, spawned_by),
    )


def _delegate(db, parent_plan: str, worker: str, title: str) -> str:
    step = service.add_node(
        parent_plan, kind="task", title=title, acceptance_criteria="Reviewed",
        spawn_kind="claude-code", db=db,
    )
    service.transition_node(step.id, "active", db=db)
    _thread(db, worker, spawned_by="parent")
    service.ensure_plan("thread", worker, db=db)
    mutations.attach_spawned_thread(step.id, worker, db=db)
    return step.id


def _leaf(db, worker: str, title: str, state: str = "pending") -> str:
    plan = service.ensure_plan("thread", worker, db=db)
    node_id = service.add_node(
        plan.id, kind="task", title=title, acceptance_criteria=f"{title} done", db=db,
    ).id
    if state in ("active", "review_requested"):
        service.transition_node(node_id, "active", db=db)
    if state == "review_requested":
        mutations.request_review(node_id, evidence=f"{title} proven", actor_id=worker, db=db)
    if state == "review_requested_bare":
        service.transition_node(node_id, "active", db=db)
        service._transition(
            db, node_id, "review_requested", actor_id=worker, provenance="test",
            verification_result=None, verified_by=None,
        )
    return node_id


def _harvested(db, step_id: str, worker: str) -> None:
    """The harvest that put the worker's returned steps up for the parent's verdict."""
    plan = service.ensure_plan("thread", worker, db=db)
    service._event(
        db, step_id, "child_plan_completed", actor_id=None,
        provenance="plan.delegation_harvest",
        payload={"child_plan_id": plan.id, "attachment_event_id": "attach"},
    )


def _legacy_db(path):
    """Finished delegated steps whose worker steps the old verdicts left open."""
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        initialize_schema(db)
        _thread(db, "parent")
        parent = service.ensure_plan("thread", "parent", db=db)
        ids: dict[str, str] = {}
        ids["accepted"] = _delegate(db, parent.id, "w-accepted", "Accepted")
        ids["returned"] = _leaf(db, "w-accepted", "returned", "review_requested")
        ids["no_evidence"] = _leaf(db, "w-accepted", "no evidence", "review_requested_bare")
        _harvested(db, ids["accepted"], "w-accepted")
        ids["unreturned"] = _leaf(db, "w-accepted", "unreturned", "active")
        ids["failed"] = _delegate(db, parent.id, "w-failed", "Failed")
        ids["failed_leaf"] = _leaf(db, "w-failed", "failed leaf", "active")
        ids["closed"] = _delegate(db, parent.id, "w-closed", "Closed")
        ids["closed_leaf"] = _leaf(db, "w-closed", "closed leaf")
        # A step the worker started whose own step is done is open work too.
        ids["branch"] = service.add_node(
            service.ensure_plan("thread", "w-failed", db=db).id, kind="phase",
            title="branch", acceptance_criteria="x", db=db,
        ).id
        service.transition_node(ids["branch"], "active", db=db)
        branch_step = service.add_node(
            service.ensure_plan("thread", "w-failed", db=db).id, kind="task",
            title="branch step", acceptance_criteria="x", parent_id=ids["branch"], db=db,
        ).id
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (branch_step,))
        # A reused worker still working for another open step is left alone.
        ids["reused_done"] = _delegate(db, parent.id, "w-reused", "Reused done")
        ids["reused_leaf"] = _leaf(db, "w-reused", "reused leaf", "active")
        ids["reused_open"] = service.add_node(
            parent.id, kind="task", title="Reused open", acceptance_criteria="x",
            spawn_kind="claude-code", db=db,
        ).id
        with raw_plan_seed(db):
            db.execute(
                "UPDATE plan_nodes SET spawned_thread_id='w-reused' WHERE id=?",
                (ids["reused_open"],),
            )
        worker_plan = service.ensure_plan("thread", "w-failed", db=db)
        db.execute(
            """INSERT INTO plan_node_attempts (id,plan_id,node_id,executor_thread_id,
                   requirements_revision,attempt_ordinal,provenance,selected_at)
               VALUES ('held',?,?,'w-failed',1,1,'test','2026-09-20T00:00:00+00:00')""",
            (worker_plan.id, ids["failed_leaf"]),
        )
        with raw_plan_seed(db):
            for key, state in (
                ("accepted", "accepted"), ("failed", "failed"),
                ("closed", "closed"), ("reused_done", "accepted"),
            ):
                db.execute("UPDATE plan_nodes SET state=? WHERE id=?", (state, ids[key]))
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=60")
        db.commit()
    return ids


def _state(db, node_id: str) -> str:
    return db.execute("SELECT state FROM plan_nodes WHERE id=?", (node_id,)).fetchone()["state"]


def _close_reason(db, node_id: str) -> str | None:
    row = db.execute(
        """SELECT json_extract(payload_json,'$.reason') AS reason, provenance
             FROM plan_node_events WHERE node_id=? AND new_state='closed'""",
        (node_id,),
    ).fetchone()
    assert row["provenance"] == _PROVENANCE
    return row["reason"]


def _heard(db) -> tuple[int, int]:
    journal = db.execute(
        "SELECT COUNT(*) FROM context_change_journal WHERE change_kind<>'plan.status'"
    ).fetchone()[0]
    messages = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return journal, messages


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 120


def test_accepted_parent_accepts_what_returned_and_stands_down_the_rest(tmp_path):
    path = tmp_path / "steps.db"
    ids = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 60, SCHEMA_VERSION)
        assert _state(db, ids["returned"]) == "accepted"
        verified = db.execute(
            """SELECT json_extract(payload_json,'$.verified_by') FROM plan_node_events
                WHERE node_id=? AND new_state='accepted'""",
            (ids["returned"],),
        ).fetchone()[0]
        assert verified == _PROVENANCE
        for key in ("unreturned", "no_evidence"):
            assert _state(db, ids[key]) == "closed", key
            assert _close_reason(db, ids[key]) == worker_retirement.STOOD_DOWN, key


def test_failed_parent_fails_and_closed_parent_closes_its_worker_steps(tmp_path):
    path = tmp_path / "steps.db"
    ids = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row

        migrate_to_current(db, SCHEMA_VERSION)

        assert _state(db, ids["failed_leaf"]) == "failed"
        assert _state(db, ids["branch"]) == "failed"
        assert _state(db, ids["closed_leaf"]) == "closed"
        assert _close_reason(db, ids["closed_leaf"]) == worker_retirement.CLOSED_WITH_DELEGATION


def test_a_settled_step_gives_up_its_selection(tmp_path):
    path = tmp_path / "steps.db"
    _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row

        migrate_to_current(db, SCHEMA_VERSION)

        row = db.execute(
            "SELECT released_at, release_reason FROM plan_node_attempts WHERE id='held'"
        ).fetchone()
        assert row["released_at"] is not None
        assert row["release_reason"] == "settled_by_schema_61"


def test_a_worker_still_held_by_an_open_step_is_left_alone(tmp_path):
    path = tmp_path / "steps.db"
    ids = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row

        migrate_to_current(db, SCHEMA_VERSION)

        assert _state(db, ids["reused_leaf"]) == "active"


def test_the_step_tells_no_one(tmp_path):
    path = tmp_path / "steps.db"
    _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        heard = _heard(db)

        migrate_to_current(db, SCHEMA_VERSION)

        assert _heard(db) == heard
        leaked = db.execute(
            """SELECT COUNT(*) FROM plan_node_events
                WHERE event='state_transition' AND created_at >= (
                      SELECT MIN(created_at) FROM plan_node_events WHERE provenance=?)
                  AND provenance<>?""",
            (_PROVENANCE, _PROVENANCE),
        ).fetchone()[0]
        assert leaked == 0


def test_rerunning_the_step_settles_nothing(tmp_path):
    from infra.migration_60_to_61_settle_orphaned_worker_steps import migrate_60_to_61

    path = tmp_path / "steps.db"
    _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)
        count = "SELECT COUNT(*) FROM plan_node_events"
        before = db.execute(count).fetchone()[0]

        migrate_60_to_61(db)

        assert db.execute(count).fetchone()[0] == before
