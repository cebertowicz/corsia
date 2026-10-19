"""Schema v51 separates roster concurrency from generic thread writes."""

from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
import json
import sqlite3

import pytest

from infra.conductor import set_scope_coordinator
from infra.database import get_db
from infra.database_migrations import migrate_to_current
from infra.fresh_threads import create_fresh_thread
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION
from tools.agent_roster_update import AgentRosterEntryTool, AgentRosterUpdateTool


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 128


def test_roster_revision_migration_backfills_existing_threads(tmp_path):
    path = tmp_path / "roster-revision.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, updated_at TEXT NOT NULL)")
        db.execute("INSERT INTO threads VALUES ('thread', 'previous-revision')")
        db.execute("PRAGMA user_version=50")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, 51)
        row = db.execute(
            "SELECT agent_roster_updated_at FROM threads WHERE id='thread'"
        ).fetchone()
        assert (result.migrated, result.from_version, result.to_version) == (True, 50, 51)
        assert row == ("previous-revision",)
        assert not migrate_to_current(db, 51).migrated


def test_fresh_and_migrated_roster_revision_columns_match(tmp_path):
    with closing(sqlite3.connect(":memory:")) as fresh:
        fresh.executescript(SCHEMA_SQL)
        fresh_column = next(
            row for row in fresh.execute("PRAGMA table_info(threads)")
            if row[1] == "agent_roster_updated_at"
        )

    path = tmp_path / "migrated-roster-revision.db"
    with closing(sqlite3.connect(path)) as legacy:
        legacy.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, updated_at TEXT NOT NULL)")
        legacy.execute("PRAGMA user_version=50")
        legacy.commit()
    with closing(sqlite3.connect(path)) as migrated:
        migrate_to_current(migrated, 51)
        migrated_column = next(
            row for row in migrated.execute("PRAGMA table_info(threads)")
            if row[1] == "agent_roster_updated_at"
        )

    assert migrated_column[1:] == fresh_column[1:]


def test_fresh_threads_start_with_a_roster_revision():
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(SCHEMA_SQL)
        db.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) VALUES ('thread','user','t','t')"
        )
        assert db.execute(
            "SELECT agent_roster_updated_at FROM threads WHERE id='thread'"
        ).fetchone()[0]


@pytest.mark.asyncio
async def test_migrated_database_gives_new_threads_a_usable_roster_revision(isolated_db):
    db = get_db()
    try:
        db.execute("ALTER TABLE threads DROP COLUMN agent_roster_updated_at")
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("PRAGMA user_version=50")
        db.commit()
        migrate_to_current(db, SCHEMA_VERSION)
        conductor = create_fresh_thread(user_id="user", thread_id="conductor", db=db)["id"]
        target = create_fresh_thread(user_id="user", thread_id="target", db=db)["id"]
        assert set_corsia_agent(conductor, db=db)
    finally:
        db.close()

    entry = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))
    assert entry["updated_at"]
    promoted = json.loads(await AgentRosterUpdateTool().execute(
        target, entry["updated_at"], responsibility="Owns migration coverage.",
        _thread_id=conductor,
    ))
    assert promoted == {"ok": True, "changed": True, "thread_id": target}
