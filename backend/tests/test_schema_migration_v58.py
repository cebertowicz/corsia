"""Schema v58 turns blockers placed for anyone but the user into waits."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from infra.thread_updates import delete_thread_updates
from plans import admission_projection, holds, service
from tests.plan_writer_authorizer import raw_plan_seed

_ARCHIVED = "2026-09-20T10:00:00.000000+00:00"


def _node(db, plan_id: str, title: str) -> str:
    return service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} done", db=db,
    ).id


def _legacy_block(db, node_id: str, owner_kind: str, reason: str) -> str:
    """A blocker hold as ``block_node`` placed it before waits, for any owner."""
    holds.place_hold(
        node_id, kind="blocked", reason_key="legacy", reason=reason,
        owner_kind=owner_kind, owner_id=None,
        expected_revision=service._node_row(db, node_id)["revision"],
        actor_id="owner", provenance="tool.plans", db=db,
    )
    return db.execute(
        "SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL",
        (node_id,),
    ).fetchone()["id"]


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
            "thread", "external", "system", "review", "user", "settled", "archived",
        )}
        held = {
            key: _legacy_block(db, ids[key], owner, f"Needs {key}")
            for key, owner in (
                ("thread", "thread"), ("external", "external"), ("system", "system"),
                ("review", "system"), ("user", "user"), ("settled", "external"), ("archived", "external"),
            )
        }
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (ids["settled"],))
            db.execute(
                "UPDATE plan_nodes SET state='review_requested' WHERE id=?", (ids["review"],),
            )
            db.execute(
                "UPDATE plan_nodes SET archived_at=? WHERE id=?", (_ARCHIVED, ids["archived"]),
            )
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=57")
        db.commit()
    return ids, held


def _cleared(db, hold_id: str) -> bool:
    return db.execute(
        "SELECT cleared_at FROM plan_node_holds WHERE id=?", (hold_id,),
    ).fetchone()[0] is not None


def _armed(db, node_id: str) -> list[tuple]:
    return db.execute(
        "SELECT id, condition_type, condition_value, owner_thread_id FROM expected_event_watches"
        " WHERE plan_node_id=? AND status='armed'",
        (node_id,),
    ).fetchall()


def _blocked_payload(db, node_id: str) -> dict:
    row = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='watch_armed'",
        (node_id,),
    ).fetchone()
    return json.loads(row[0])["blocked"]


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 152


def test_only_open_blockers_on_anyone_but_the_user_become_waits(tmp_path):
    path = tmp_path / "blocked.db"
    ids, held = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        started = datetime.now(timezone.utc)

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 57, SCHEMA_VERSION)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated

        # A task in review is still open: its blocker would otherwise read red.
        for key in ("thread", "external", "system", "review"):
            assert _cleared(db, held[key]), key
            [(_id, kind, value, owner)] = _armed(db, ids[key])
            due = datetime.fromisoformat(value)
            assert kind == "deadline" and owner == "owner", key
            assert started + timedelta(hours=23) < due <= datetime.now(timezone.utc) + timedelta(hours=24)
            assert _blocked_payload(db, ids[key]) == {
                "reason_key": "legacy", "reason": f"Needs {key}",
                "owner_kind": "system" if key == "review" else key, "owner_id": None,
            }
        # The user's own blocker stays a hold; settled and archived work is left alone.
        for key in ("user", "settled", "archived"):
            assert not _cleared(db, held[key]), key
            assert _armed(db, ids[key]) == [], key


def test_a_moved_blocker_reads_as_its_wait(tmp_path):
    path = tmp_path / "blocked.db"
    ids, _held = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)

        projected = admission_projection.project(ids["external"], db=db)

        assert projected["condition"] == "waiting"
        [cause] = projected["causes"]
        assert cause["owner_kind"] == "external"
        assert cause["reason"].startswith("Until ")
        assert cause["reason"].endswith(" · Needs external")


def test_a_plan_no_thread_owns_keeps_its_blocker_holds(tmp_path):
    """A wait there would wake no one, and the runtime would never arm one."""
    path = tmp_path / "scoped.db"
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        initialize_schema(db)
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')"
        )
        plan = service.ensure_plan("project_scope", "project", db=db)
        node_id = _node(db, plan.id, "scoped")
        hold_id = _legacy_block(db, node_id, "external", "Needs external")
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=57")
        db.commit()

        migrate_to_current(db, SCHEMA_VERSION)

        assert not _cleared(db, hold_id)
        assert _armed(db, node_id) == []


def test_a_gone_owner_s_plan_is_retired_and_its_blockers_stay(tmp_path):
    """Delete means delete for threads deleted or purged before #2647 too."""
    path = tmp_path / "gone.db"
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        initialize_schema(db)
        gone = {}
        for owner, purged in (("deleted", False), ("purged", True), ("live", False)):
            db.execute(
                "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
                " VALUES (?,'user',?,'t','t')", (owner, owner),
            )
            plan = service.ensure_plan("thread", owner, db=db)
            node_id = _node(db, plan.id, owner)
            gone[owner] = (plan.id, node_id, _legacy_block(db, node_id, "external", "Needs external"))
            if purged:
                db.execute("UPDATE threads SET lifecycle_state='purged' WHERE id=?", (owner,))
        # The blocker above projected an update card; a real delete removes a
        # thread's cards before the thread row (core.thread_family), so do too.
        delete_thread_updates(db, "deleted")
        db.execute("DELETE FROM threads WHERE id='deleted'")
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=57")
        db.commit()

        migrate_to_current(db, SCHEMA_VERSION)

        for owner in ("deleted", "purged"):
            plan_id, node_id, hold_id = gone[owner]
            assert db.execute("SELECT state FROM plans WHERE id=?", (plan_id,)).fetchone()[0] == "archived"
            assert db.execute(
                "SELECT json_extract(payload_json,'$.reason') FROM plan_events"
                " WHERE plan_id=? AND new_state='archived'", (plan_id,),
            ).fetchone()[0] == owner
            # Nobody is left to wake, so the blocker is not turned into a wait.
            assert not _cleared(db, hold_id) and _armed(db, node_id) == [], owner
        plan_id, node_id, hold_id = gone["live"]
        assert db.execute("SELECT state FROM plans WHERE id=?", (plan_id,)).fetchone()[0] == "active"
        assert _cleared(db, hold_id) and len(_armed(db, node_id)) == 1


def test_moving_a_worker_s_blocker_neither_wakes_nor_tells_its_parent():
    """The worker raised nothing new: the upgrade must not start the parent's Turn."""
    from tests.child_parent_notice_helpers import delegated, notices, seeded_db, wakes
    from infra.migration_57_to_58_waits_and_gone_owners import migrate_57_to_58

    with closing(seeded_db()) as db:
        _step, _plan, node = delegated(db)
        hold_id = _legacy_block(db, node.id, "external", "Needs external")
        told, woken = len(notices(db)), len(wakes(db))

        migrate_57_to_58(db)

        assert _cleared(db, hold_id)
        assert len(_armed(db, node.id)) == 1
        assert (len(notices(db)), len(wakes(db))) == (told, woken)
