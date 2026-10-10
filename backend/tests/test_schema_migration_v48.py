"""Schema v48 puts tasks closed before #1887 back onto their plans."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from plans import service

_ARCHIVED = "2026-09-20T10:00:00.000000+00:00"


def _node(db, plan_id: str, title: str, *, kind: str = "task", parent_id=None) -> str:
    return service.add_node(
        plan_id, kind=kind, title=title, acceptance_criteria=f"{title} done",
        parent_id=parent_id, db=db,
    ).id


def _archive_as(db, node_id: str, state: str) -> None:
    """Write the row the way a pre-#1887 close (or an explicit archive) left it."""
    db.execute(
        "UPDATE plan_nodes SET state=?, archived_at=? WHERE id=?",
        (state, _ARCHIVED, node_id),
    )


def _legacy_db(path):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        initialize_schema(db)
        db.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
            " VALUES ('owner','user','Owner','t','t')"
        )
        plan = service.ensure_plan("thread", "owner", db=db)
        ids = {
            "closed": _node(db, plan.id, "Closed"),
            "closed_phase": _node(db, plan.id, "Closed phase", kind="phase"),
            "archived_phase": _node(db, plan.id, "Archived phase", kind="phase"),
            "accepted": _node(db, plan.id, "Accepted"),
        }
        ids["closed_step"] = _node(db, plan.id, "Closed step", parent_id=ids["closed_phase"])
        ids["under_archived"] = _node(
            db, plan.id, "Closed under archived", parent_id=ids["archived_phase"],
        )
        for key in ("closed", "closed_phase", "closed_step", "under_archived"):
            _archive_as(db, ids[key], "closed")
        _archive_as(db, ids["archived_phase"], "pending")
        _archive_as(db, ids["accepted"], "accepted")
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=47")
        db.commit()
    return ids


def _rows(db) -> dict[str, tuple]:
    return {
        row[0]: row[1:]
        for row in db.execute("SELECT id, archived_at, revision, updated_at FROM plan_nodes")
    }


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 103


def test_closed_tasks_come_back_and_nothing_else_does(tmp_path):
    path = tmp_path / "closed-archived.db"
    ids = _legacy_db(path)
    with closing(sqlite3.connect(path)) as db:
        before = _rows(db)

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 47, SCHEMA_VERSION)
        after = _rows(db)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated

    restored = {"closed", "closed_phase", "closed_step"}
    for key, node_id in ids.items():
        archived_at, revision, updated_at = after[node_id]
        assert (archived_at is None) == (key in restored), key
        # Only archived_at moves: revision and last activity keep their history.
        assert (revision, updated_at) == before[node_id][1:], key
