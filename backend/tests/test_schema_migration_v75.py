"""Schema v75 backfills ``checkpoints.through_at`` for pre-v74 installs (#3196)."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION


def _checkpoint(db, checkpoint_id: str, *, message_id: str, installed_at) -> None:
    db.execute(
        "INSERT INTO checkpoints (id,thread_id,history_epoch,through_seq,"
        "through_message_id,trigger,mode,status,source_harness,source_model,"
        "writer_harness,writer_model,continuity_base_revision,"
        "continuity_base_snapshot_json,started_at,installed_at) VALUES "
        "(?,'t-1',0,1,?,'manual','warm',?,'native','m','native','m',0,'{}','s',?)",
        (checkpoint_id, message_id,
         "installed" if installed_at else "captured", installed_at),
    )


def test_startup_migration_backfills_installed_rows_only(tmp_path):
    path = tmp_path / "checkpoint-through-at-v74.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,next_message_seq,"
            "created_at,updated_at) VALUES ('t-1','u','T','m',2,'t0','t0')"
        )
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES ('m-1','t-1','user','body','2026-09-19T03:00:00',1)"
        )
        _checkpoint(db, "installed", message_id="m-1", installed_at="t2")
        _checkpoint(db, "captured", message_id="m-1", installed_at=None)
        _checkpoint(db, "orphaned", message_id="gone", installed_at="t2")
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("DROP TABLE measure_files")
        db.execute("PRAGMA user_version=74")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (
            True, 74, SCHEMA_VERSION,
        )
        rows = dict(db.execute("SELECT id, through_at FROM checkpoints"))
        assert rows == {
            "installed": "2026-09-19T03:00:00", "captured": None, "orphaned": None,
        }
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
