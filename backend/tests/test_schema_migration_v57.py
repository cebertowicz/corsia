"""Schema v57 closes the submissions a review rejection failed before sent-back."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
import json
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from plans import dependencies, rejection, service
from tests.plan_writer_authorizer import raw_plan_seed

_ARCHIVED = "2026-09-20T10:00:00.000000+00:00"


def _node(db, plan_id: str, title: str) -> str:
    return service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} done", db=db,
    ).id


def _fail(db, node_id: str, *, actor_id: str = "agent", provenance: str = "tool.plans") -> None:
    """Write the failure the way the runtime's transition left it."""
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='failed' WHERE id=?", (node_id,))
    service._event(
        db, node_id, "state_transition", actor_id=actor_id, provenance=provenance,
        prior_state="review_requested", new_state="failed",
    )


def _rejected(db, node_id: str, feedback: str, **who) -> None:
    service._event(
        db, node_id, "review_rejected", actor_id=who.get("actor_id", "agent"),
        provenance=who.get("provenance", "tool.plans"), payload={"feedback": feedback},
    )


def _rejected_before_sent_back(db, node_id: str, feedback: str) -> None:
    """Pre-change rejection: record the feedback, then fail the submission."""
    _rejected(db, node_id, feedback)
    _fail(db, node_id)


def _new_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    db.execute(
        "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
        " VALUES ('owner','user','Owner','t','t')"
    )
    return db, service.ensure_plan("thread", "owner", db=db)


def _legacy_db(path):
    db, plan = _new_db(path)
    with closing(db):
        ids = {key: _node(db, plan.id, key) for key in (
            "sent_back", "no_feedback", "failed_then_rejected", "other_actor",
            "rejected_archived", "plain_failed",
        )}
        _rejected_before_sent_back(db, ids["sent_back"], "Add the screenshot")
        _rejected_before_sent_back(db, ids["no_feedback"], "")
        # Failed on its own after an earlier rejection: red is true.
        _rejected(db, ids["failed_then_rejected"], "Earlier round")
        service._event(db, ids["failed_then_rejected"], "note", actor_id="agent",
                       provenance="tool.plans", payload={})
        _fail(db, ids["failed_then_rejected"])
        # A rejection whose failure another writer made is not the rejection's.
        _rejected(db, ids["other_actor"], "Mine")
        _fail(db, ids["other_actor"], actor_id="lifecycle:owner", provenance="thread_archive")
        _rejected_before_sent_back(db, ids["rejected_archived"], "Gone")
        with raw_plan_seed(db):
            db.execute(
                "UPDATE plan_nodes SET archived_at=? WHERE id=?",
                (_ARCHIVED, ids["rejected_archived"]),
            )
        _fail(db, ids["plain_failed"])
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("PRAGMA user_version=56")
        db.commit()
    return plan.id, ids


def _rows(db) -> dict[str, tuple]:
    return {row[0]: row[1:] for row in db.execute("SELECT id, state, revision FROM plan_nodes")}


def _last_transition(db, node_id: str) -> tuple:
    row = db.execute(
        "SELECT new_state, payload_json FROM plan_node_events"
        " WHERE node_id=? AND event='state_transition'"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (node_id,),
    ).fetchone()
    return row[0], json.loads(row[1])


def _plan_revision(db, plan_id: str) -> int:
    return db.execute("SELECT revision FROM plans WHERE id=?", (plan_id,)).fetchone()[0]


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 129


def test_only_rejection_failures_close_as_sent_back(tmp_path):
    path = tmp_path / "sent_back.db"
    plan_id, ids = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        before, plan_before = _rows(db), _plan_revision(db, plan_id)

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 56, SCHEMA_VERSION)
        after = _rows(db)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated

        reasons = {
            "sent_back": f"{rejection.SENT_BACK}: Add the screenshot",
            "no_feedback": rejection.SENT_BACK,
        }
        for key, node_id in ids.items():
            if key in reasons:
                assert after[node_id] == ("closed", before[node_id][1] + 1), key
                assert _last_transition(db, node_id) == (
                    "closed", {"reason": reasons[key]},
                ), key
            else:
                assert after[node_id] == before[node_id], key
        # A caller holding the old plan revision is refused, not misled.
        assert _plan_revision(db, plan_id) > plan_before


def test_a_send_back_over_open_work_stays_failed(tmp_path):
    """A close may hold nothing open beneath it, so the runtime send-back refuses one.

    The old rejection never checked, so a rejected phase can still have work
    in progress below it. Closing it would hide that work from every walk that
    stops at a retired node; it keeps the red it already had.
    """
    db, plan = _new_db(tmp_path / "open_below.db")
    with closing(db):
        phase = service.add_node(
            plan.id, kind="phase", title="Rejected phase", acceptance_criteria="done", db=db,
        ).id
        child = service.add_node(
            plan.id, kind="task", title="Still open", acceptance_criteria="done",
            parent_id=phase, db=db,
        ).id
        _rejected_before_sent_back(db, phase, "Redo it")
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("PRAGMA user_version=56")
        db.commit()
        before = _rows(db)

        migrate_to_current(db, SCHEMA_VERSION)

        after = _rows(db)
        assert after[phase] == before[phase]
        assert after[child] == before[child]


def test_a_migrated_send_back_releases_what_it_held(tmp_path):
    """A dependent's edge on the old submission goes, as any close releases it."""
    db, plan = _new_db(tmp_path / "held.db")
    with closing(db):
        legacy = _node(db, plan.id, "Legacy submission")
        dependent = _node(db, plan.id, "After it")
        dependencies.add_dependency(dependent, legacy, db=db)
        _rejected_before_sent_back(db, legacy, "Redo it")
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("PRAGMA user_version=56")
        db.commit()

        migrate_to_current(db, SCHEMA_VERSION)

        edges = db.execute(
            "SELECT count(*) FROM plan_node_dependencies WHERE depends_on_node_id=?",
            (legacy,),
        ).fetchone()[0]
        assert edges == 0


def test_closing_a_worker_s_sent_back_step_neither_wakes_nor_tells_its_parent():
    """The rejection is old news: the upgrade must not post 'Step closed' to the parent."""
    from tests.child_parent_notice_helpers import delegated, notices, seeded_db, wakes
    from infra.migration_56_to_57_sent_back_close import migrate_56_to_57

    with closing(seeded_db()) as db:
        _step, _plan, node = delegated(db)
        _rejected_before_sent_back(db, node.id, "Tighten the test")
        told, woken = len(notices(db)), len(wakes(db))

        migrate_56_to_57(db)

        assert _last_transition(db, node.id)[0] == "closed"
        assert (len(notices(db)), len(wakes(db))) == (told, woken)
