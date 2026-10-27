"""Schema v59 turns pauses an agent placed on its own plan into scheduled tasks."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from unittest.mock import patch

import pytest

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.plan_schema import HOLD_UPDATE_GUARD_SQL
from infra.schema import SCHEMA_VERSION
from plans import admission, holds, lifecycle, service
from tests.plan_writer_authorizer import raw_plan_seed

_ARCHIVED = "2026-09-20T10:00:00.000000+00:00"
_MOVED = "Set aside before agents could pause"


def _node(db, plan_id: str, title: str) -> str:
    return service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} done", db=db,
    ).id


def _pause(db, node_id: str, actor_id: str, reason: str) -> str:
    """A pause hold as ``pause_node`` placed it before agents lost pause."""
    holds.pause_hold(
        node_id, reason, expected_revision=service._node_row(db, node_id)["revision"],
        actor_id=actor_id, provenance="tool.plans", db=db,
    )
    return db.execute(
        "SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL",
        (node_id,),
    ).fetchone()["id"]


def _legacy_user_pause(db, node_id: str) -> str:
    """A user pause recorded before holds named the user as owner_kind."""
    hold_id = _pause(db, node_id, "user:user", "Not now")
    db.execute("DROP TRIGGER restrict_plan_hold_update")
    db.execute("UPDATE plan_node_holds SET owner_kind='thread' WHERE id=?", (hold_id,))
    db.execute(HOLD_UPDATE_GUARD_SQL)
    return hold_id


#: Before an agent's plan pause needed the user's message in its Turn (#2237).
_BEFORE_RELAY = "2026-09-20T09:00:00.000000+00:00"
_AFTER_RELAY = "2026-09-26T09:00:00.000000+00:00"


def _pause_plan(db, plan_id: str, actor_id: str, *, at: str = _BEFORE_RELAY) -> None:
    revision = db.execute("SELECT revision FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
    with patch("plans.service._now", return_value=at):
        lifecycle.transition(
            plan_id, "paused", expected_revision=revision, actor_id=actor_id,
            provenance="tool.plans", db=db,
        )


def _legacy_db(path):
    """Three thread plans: one with task pauses, one the agent paused, one the user paused."""
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        initialize_schema(db)
        for thread in ("owner", "idle", "held", "relayed"):
            db.execute(
                "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
                " VALUES (?,'user','T','t','t')",
                (thread,),
            )
        tasks = service.ensure_plan("thread", "owner", db=db)
        ids = {key: _node(db, tasks.id, key) for key in (
            "agent", "user", "legacy_user", "settled", "archived",
        )}
        held = {
            "agent": _pause(db, ids["agent"], "owner", "Waiting on a design"),
            "user": _pause(db, ids["user"], "user:user", "Hold on"),
            "legacy_user": _legacy_user_pause(db, ids["legacy_user"]),
            "settled": _pause(db, ids["settled"], "owner", "Later"),
            "archived": _pause(db, ids["archived"], "owner", "Later"),
        }
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (ids["settled"],))
            db.execute(
                "UPDATE plan_nodes SET archived_at=? WHERE id=?", (_ARCHIVED, ids["archived"]),
            )
        idle = service.ensure_plan("thread", "idle", db=db)
        ids["idle_first"] = _node(db, idle.id, "idle first")
        ids["idle_second"] = _node(db, idle.id, "idle second")
        # Paused mid-task, as agents did: the pause released the task's slot.
        admission.activate(
            ids["idle_first"], expected_revision=service._node_row(db, ids["idle_first"])["revision"],
            actor_id="idle", provenance="tool.plans", db=db,
        )
        _pause_plan(db, idle.id, "idle")
        user_paused = service.ensure_plan("thread", "held", db=db)
        ids["user_plan"] = _node(db, user_paused.id, "user plan")
        _pause_plan(db, user_paused.id, "user:user")
        # Paused by its agent once a plan pause needed the user's message: the
        # agent passed on the user's request.
        relayed = service.ensure_plan("thread", "relayed", db=db)
        ids["relayed_task"] = _node(db, relayed.id, "relayed task")
        _pause_plan(db, relayed.id, "relayed", at=_AFTER_RELAY)
        ids["idle_plan"], ids["user_paused_plan"] = idle.id, user_paused.id
        ids["relayed_plan"] = relayed.id
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=58")
        db.commit()
    return ids, held


def _cleared(db, hold_id: str) -> bool:
    return db.execute(
        "SELECT cleared_at FROM plan_node_holds WHERE id=?", (hold_id,),
    ).fetchone()[0] is not None


def _armed(db, node_id: str) -> list[tuple]:
    return db.execute(
        "SELECT condition_type, condition_value, owner_thread_id, expected_event"
        " FROM expected_event_watches WHERE plan_node_id=? AND status='armed'",
        (node_id,),
    ).fetchall()


def _armed_payload(db, node_id: str) -> dict:
    row = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='watch_armed'",
        (node_id,),
    ).fetchone()
    return json.loads(row[0])


def _provenance(db, node_id: str) -> str:
    return db.execute(
        "SELECT provenance FROM plan_node_events WHERE node_id=? AND event='watch_armed'",
        (node_id,),
    ).fetchone()[0]


def _plan_state(db, plan_id: str) -> str:
    return db.execute("SELECT state FROM plans WHERE id=?", (plan_id,)).fetchone()[0]


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 151


def test_only_the_agents_own_open_pauses_become_scheduled_tasks(tmp_path):
    path = tmp_path / "paused.db"
    ids, held = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        started = datetime.now(timezone.utc)

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 58, SCHEMA_VERSION)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
        assert _cleared(db, held["agent"])
        [(kind, value, owner, event)] = _armed(db, ids["agent"])
        due = datetime.fromisoformat(value)
        assert (kind, owner, event) == ("deadline", "owner", "scheduled")
        assert started + timedelta(hours=1) < due <= datetime.now(timezone.utc) + timedelta(days=7)
        payload = _armed_payload(db, ids["agent"])
        assert payload["scheduled"] == {"reason": "Waiting on a design"}
        # A move, not news: its provenance tells no one (plan_events.from_schema_step).
        assert _provenance(db, ids["agent"]) == "migration.58_to_59"
        # The user's pause stays; settled and archived work is left alone.
        for key in ("user", "legacy_user", "settled", "archived"):
            assert not _cleared(db, held[key]), key
            assert _armed(db, ids[key]) == [], key


def test_an_older_user_pause_is_recorded_as_the_users(tmp_path):
    """One reading of a user pause from here on: owner_kind='user'."""
    path = tmp_path / "paused.db"
    _ids, held = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        migrate_to_current(db, SCHEMA_VERSION)

        owners = db.execute(
            "SELECT owner_kind, owner_id FROM plan_node_holds WHERE id=?",
            (held["legacy_user"],),
        ).fetchone()
        assert tuple(owners) == ("user", "user:user")
        # Holds stay immutable after the one rewrite.
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                "UPDATE plan_node_holds SET owner_kind='thread' WHERE id=?",
                (held["legacy_user"],),
            )


def test_the_step_tells_no_agent_anything(tmp_path):
    """Lifting a plan finds its tasks already scheduled, so nothing is selected."""
    path = tmp_path / "paused.db"
    ids, _held = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        before = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

        migrate_to_current(db, SCHEMA_VERSION)

        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before
        selected = db.execute(
            "SELECT COUNT(*) FROM plan_node_attempts a JOIN plan_nodes n ON n.id=a.node_id"
            " WHERE n.plan_id=? AND a.released_at IS NULL",
            (ids["idle_plan"],),
        ).fetchone()[0]
        assert selected == 0


def test_a_plan_the_agent_paused_resumes_with_its_tasks_scheduled(tmp_path):
    path = tmp_path / "paused.db"
    ids, _held = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        migrate_to_current(db, SCHEMA_VERSION)

        assert _plan_state(db, ids["idle_plan"]) == "active"
        for key in ("idle_first", "idle_second"):
            [(kind, _value, owner, _event)] = _armed(db, ids[key])
            assert (kind, owner) == ("deadline", "idle"), key
            assert _armed_payload(db, ids[key])["scheduled"] == {"reason": _MOVED}, key
        # The user's whole-plan pause is the user's.
        assert _plan_state(db, ids["user_paused_plan"]) == "paused"
        assert _armed(db, ids["user_plan"]) == []


def test_a_plan_pause_that_passed_on_the_user_s_request_stays(tmp_path):
    path = tmp_path / "paused.db"
    ids, _held = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        migrate_to_current(db, SCHEMA_VERSION)

        assert _plan_state(db, ids["relayed_plan"]) == "paused"
        assert _armed(db, ids["relayed_task"]) == []


def test_rerunning_the_step_finds_nothing_left_to_move(tmp_path):
    from infra.migration_58_to_59_agent_pauses_scheduled import migrate_58_to_59

    path = tmp_path / "paused.db"
    _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        migrate_to_current(db, SCHEMA_VERSION)
        count = "SELECT COUNT(*) FROM expected_event_watches WHERE status='armed'"
        armed = db.execute(count).fetchone()[0]

        migrate_58_to_59(db)

        assert armed == 3
        assert db.execute(count).fetchone()[0] == armed


def test_moved_pauses_are_spread_over_the_week_oldest_first(tmp_path):
    path = tmp_path / "paused.db"
    ids, _held = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        migrate_to_current(db, SCHEMA_VERSION)

        # The plan pause is dated before the relay rule, so before the hold.
        order = ("idle_first", "idle_second", "agent")
        dues = [datetime.fromisoformat(_armed(db, ids[key])[0][1]) for key in order]
        assert dues == sorted(dues) and len(set(dues)) == len(dues)
        spacing = {round((later - earlier).total_seconds()) for earlier, later in zip(dues, dues[1:])}
        assert len(spacing) == 1
        assert dues[-1] - dues[0] > timedelta(days=3)


def test_a_pause_on_an_archived_plan_stays_where_it_is(tmp_path):
    """A gone owner's plan is archived by schema 58; nothing there is scheduled."""
    path = tmp_path / "archived.db"
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        initialize_schema(db)
        db.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
            " VALUES ('gone','user','T','t','t')"
        )
        plan = service.ensure_plan("thread", "gone", db=db)
        node_id = _node(db, plan.id, "left behind")
        hold_id = _pause(db, node_id, "gone", "Later")
        db.execute("UPDATE plans SET state='archived' WHERE id=?", (plan.id,))
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=58")
        db.commit()

        migrate_to_current(db, SCHEMA_VERSION)

        assert not _cleared(db, hold_id)
        assert _armed(db, node_id) == []
