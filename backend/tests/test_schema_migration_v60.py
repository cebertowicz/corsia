"""Schema v60 gives every armed event wait on open plan work a deadline (W4)."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from automation import watches
from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from plans import mutations, service
from tests.plan_scene import _artifact
from tests.plan_writer_authorizer import raw_plan_seed

_ARCHIVED = "2026-09-20T10:00:00.000000+00:00"
_CHECKS = "github.check_suite.completed"


def _thread(db, thread_id: str, *, spawned_by: str | None = None) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,title,spawned_by,created_at,updated_at)"
        " VALUES (?,'user','T',?,'t','t')",
        (thread_id, spawned_by),
    )


def _node(db, plan_id: str, title: str) -> str:
    return service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} done", db=db,
    ).id


def _event_wait(db, plan_id: str, node_id: str, owner: str, pr: str, armed_at: str) -> str:
    """An event wait as ``arm_watch`` armed one before every wait had a deadline."""
    watch = watches.arm_watch(
        _artifact(db, external_id=pr, owner=owner),
        {"type": "event", "value": "ci.complete"},
        owner_thread=owner, expected_event=_CHECKS,
        plan_id=plan_id, plan_node_id=node_id, db=db,
    )
    db.execute("UPDATE expected_event_watches SET armed_at=? WHERE id=?", (armed_at, watch.id))
    return watch.id


def _legacy_db(path):
    """Open event waits on live plans, plus every kind that must stay as it is."""
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        initialize_schema(db)
        for thread in ("owner", "parent", "archived", "gone"):
            _thread(db, thread)
        _thread(db, "worker", spawned_by="parent")
        ids: dict[str, str] = {}
        owner = service.ensure_plan("thread", "owner", db=db)
        for key in ("later", "older", "settled", "dated"):
            ids[key] = _node(db, owner.id, key)
        watched = {
            "later": _event_wait(db, owner.id, ids["later"], "owner", "o/r#2", "2026-09-20T02:00:00+00:00"),
            "older": _event_wait(db, owner.id, ids["older"], "owner", "o/r#1", "2026-09-20T01:00:00+00:00"),
            "settled": _event_wait(db, owner.id, ids["settled"], "owner", "o/r#3", "2026-09-20T03:00:00+00:00"),
        }
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (ids["settled"],))
        watched["dated"] = watches.arm_watch(
            ids["dated"], {"type": "deadline", "value": "2026-10-10T00:00:00+00:00"},
            owner_thread="owner", plan_id=owner.id, plan_node_id=ids["dated"], db=db,
        ).id
        # A worker's wait: moving it is not news its parent hears.
        parent = service.ensure_plan("thread", "parent", db=db)
        step = service.add_node(
            parent.id, kind="task", title="Delegate", acceptance_criteria="Reviewed",
            spawn_kind="claude-code", db=db,
        )
        service.transition_node(step.id, "active", db=db)
        worker = service.ensure_plan("thread", "worker", db=db)
        ids["worker"] = _node(db, worker.id, "worker step")
        mutations.attach_spawned_thread(step.id, "worker", db=db)
        watched["worker"] = _event_wait(
            db, worker.id, ids["worker"], "worker", "o/r#4", "2026-09-20T04:00:00+00:00",
        )
        # An archived plan and a gone owner's plan keep their waits.
        archived = service.ensure_plan("thread", "archived", db=db)
        ids["archived"] = _node(db, archived.id, "archived")
        watched["archived"] = _event_wait(
            db, archived.id, ids["archived"], "archived", "o/r#5", "2026-09-20T05:00:00+00:00",
        )
        db.execute("UPDATE plans SET state='archived' WHERE id=?", (archived.id,))
        gone = service.ensure_plan("thread", "gone", db=db)
        ids["gone"] = _node(db, gone.id, "gone")
        watched["gone"] = _event_wait(
            db, gone.id, ids["gone"], "gone", "o/r#6", "2026-09-20T06:00:00+00:00",
        )
        db.execute("UPDATE threads SET lifecycle_state='purged' WHERE id='gone'")
        # A wait no plan holds is not plan work.
        watched["unbound"] = watches.arm_watch(
            _artifact(db, external_id="o/r#7", owner="owner"),
            {"type": "event", "value": "ci.complete"},
            owner_thread="owner", expected_event=_CHECKS, db=db,
        ).id
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=59")
        db.commit()
    return ids, watched


def _watch(db, watch_id: str) -> sqlite3.Row:
    return db.execute(
        "SELECT * FROM expected_event_watches WHERE id=?", (watch_id,),
    ).fetchone()


def _parent_heard(db) -> tuple[int, int]:
    notices = db.execute(
        """SELECT COUNT(*) FROM context_change_journal j JOIN plans p ON p.id=j.object_id
           WHERE j.object_kind='plan' AND p.owner_id='parent' AND j.change_kind<>'plan.status'"""
    ).fetchone()[0]
    wakes = db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='parent' AND id LIKE 'plan-wake:%'"
    ).fetchone()[0]
    return notices, wakes


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 137


def test_open_event_waits_get_a_deadline_and_keep_their_event(tmp_path):
    path = tmp_path / "waits.db"
    ids, watched = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        heard = _parent_heard(db)
        started = datetime.now(timezone.utc)

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 59, SCHEMA_VERSION)
        for key in ("older", "later", "worker"):
            row = _watch(db, watched[key])
            due = datetime.fromisoformat(row["condition_value"])
            assert (row["status"], row["condition_type"]) == ("armed", "deadline"), key
            assert row["expected_event"] == _CHECKS, key
            assert started + timedelta(hours=1) < due <= datetime.now(timezone.utc) + timedelta(days=7), key
            event = db.execute(
                """SELECT payload_json, provenance FROM plan_node_events
                   WHERE node_id=? AND event='watch_deadline_given'""",
                (ids[key],),
            ).fetchone()
            payload = json.loads(event["payload_json"])
            assert payload["watch_id"] == watched[key] and "moved" not in payload, key
            assert event["provenance"] == "migration.59_to_60", key
        # A move, not news: the worker's parent is neither told nor woken.
        assert _parent_heard(db) == heard


def test_settled_archived_gone_dated_and_unbound_waits_stay(tmp_path):
    path = tmp_path / "waits.db"
    _ids, watched = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        before = {key: dict(_watch(db, watched[key])) for key in (
            "settled", "archived", "gone", "unbound", "dated",
        )}

        migrate_to_current(db, SCHEMA_VERSION)

        for key, row in before.items():
            assert dict(_watch(db, watched[key])) == row, key


def test_moved_waits_are_spread_over_the_week_oldest_first(tmp_path):
    path = tmp_path / "waits.db"
    _ids, watched = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)

        dues = [
            datetime.fromisoformat(_watch(db, watched[key])["condition_value"])
            for key in ("older", "later", "worker")
        ]
        assert dues == sorted(dues) and len(set(dues)) == len(dues)
        spacing = {round((b - a).total_seconds()) for a, b in zip(dues, dues[1:])}
        assert len(spacing) == 1
        assert dues[-1] - dues[0] > timedelta(days=3)


def test_rerunning_the_step_moves_nothing(tmp_path):
    from infra.migration_59_to_60_event_waits_deadline import migrate_59_to_60

    path = tmp_path / "waits.db"
    _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)
        snapshot = "SELECT id,condition_type,condition_value FROM expected_event_watches ORDER BY id"
        before = [tuple(row) for row in db.execute(snapshot).fetchall()]
        events = "SELECT COUNT(*) FROM plan_node_events WHERE event='watch_deadline_given'"
        given = db.execute(events).fetchone()[0]

        migrate_59_to_60(db)

        assert [tuple(row) for row in db.execute(snapshot).fetchall()] == before
        assert db.execute(events).fetchone()[0] == given == 3
