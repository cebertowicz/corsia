"""Shared fixtures for bounded checkpoint-retrieval tests.

Retrieval is read-only over rows that capture and install already wrote, so
these build the smallest database that answers a read: threads carrying the
owner and history epoch retrieval filters on, plus checkpoint rows in the
state install leaves them.

Two owners exist from the start. Retrieval is a cross-thread read surface, so
"whose generation is this" is part of the contract under test, not a variation
a later test bolts on.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

OWNER_USER = "u"
OTHER_USER = "other-user"


@pytest.fixture
def fresh_db():
    """One in-memory database carrying only what retrieval reads.

    ``t1`` belongs to :data:`OWNER_USER` and ``t-foreign`` to
    :data:`OTHER_USER`, so any read can be checked for crossing an owner
    boundary without further setup.
    """
    from infra.checkpoint_schema import CHECKPOINT_SCHEMA_SQL
    from infra.continuity_schema import CONTINUITY_SCHEMA_SQL
    from infra.db_fts import _init_checkpoint_entry_search_fts, _init_checkpoints_fts

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, user_id TEXT, title TEXT,"
        " history_epoch INTEGER NOT NULL DEFAULT 0)"
    )
    db.executescript(CHECKPOINT_SCHEMA_SQL)
    db.executescript(CONTINUITY_SCHEMA_SQL)
    # Search runs through the index, so the fixture builds the same one the
    # fresh schema does rather than a convenient substitute.
    _init_checkpoints_fts(db)
    _init_checkpoint_entry_search_fts(db)
    db.executemany(
        "INSERT INTO threads (id,user_id,title,history_epoch) VALUES (?,?,?,0)",
        [
            ("t1", OWNER_USER, "T"),
            ("t-foreign", OTHER_USER, "Someone else's thread"),
        ],
    )
    db.commit()
    try:
        yield db
    finally:
        db.close()


def _install_checkpoint(
    db,
    *,
    thread_id: str,
    narrative: str,
    continuity: list[dict] | None = None,
    history_epoch: int = 0,
    status: str = "installed",
    checkpoint_id: str | None = None,
) -> str:
    """Insert one checkpoint row the way capture and install leave it."""
    cid = checkpoint_id or f"cp-{thread_id}-{history_epoch}-{narrative[:8]}"
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,"
        " mode,status,source_harness,source_model,writer_harness,writer_model,"
        " continuity_base_revision,continuity_base_snapshot_json,started_at) "
        "VALUES (?,?,?,?,?,'pressure','cold','running',"
        " 'native','m','native','m',0,'[]',?)",
        (cid, thread_id, history_epoch, 100, "m-100", "2026-09-01T00:00:00"),
    )
    entries = [
        entry if "entry_id" in entry else {
            "entry_id": index,
            "field": "facts",
            "body": entry,
            "revision": 1,
        }
        for index, entry in enumerate(continuity or [], start=1)
    ]
    db.execute(
        "UPDATE checkpoints SET status=?, raw_result=?, "
        "continuity_snapshot_json=?, captured_at=?, installed_at=? WHERE id=?",
        (
            status,
            narrative,
            json.dumps(entries),
            "2026-09-01T00:05:00",
            "2026-09-01T00:05:01",
            cid,
        ),
    )
    if status == "installed":
        from infra.checkpoint_entry_index import index_checkpoint_entries

        index_checkpoint_entries(db, cid)
    for entry in entries:
        db.execute(
            "INSERT INTO continuity_entries "
            "(thread_id,history_epoch,entry_id,field,body,revision,state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?, 'active', ?, ?) "
            "ON CONFLICT(thread_id,history_epoch,entry_id) DO UPDATE SET "
            "field=excluded.field,body=excluded.body,revision=excluded.revision,"
            "state='active',retired_reason=NULL,retired_source_ref=NULL,updated_at=excluded.updated_at",
            (
                thread_id,
                history_epoch,
                entry["entry_id"],
                entry["field"],
                json.dumps(entry["body"], sort_keys=True, separators=(",", ":")),
                entry.get("revision", 1),
                "2026-09-01T00:05:00",
                "2026-09-01T00:05:00",
            ),
        )
    db.commit()
    return cid
