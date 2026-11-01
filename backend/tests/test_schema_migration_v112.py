"""Schema v112 adds image-region anchors to document comments (DCOL-FR13.3/13.4)."""

from __future__ import annotations

import sqlite3

import pytest

from infra.database_migrations import MIGRATIONS
from infra.migration_111_to_112_image_regions import REGION_COLUMNS, migrate_111_to_112
from infra.migration_133_to_134_doc_stranded_suggestions import migrate_133_to_134
from infra.migration_134_to_135_doc_decision_bulk import migrate_134_to_135
from infra.migration_149_to_150_doc_insert_after import migrate_149_to_150
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION

_V111_DOC_COMMENTS = """
    CREATE TABLE doc_comments (
        id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, parent_id TEXT, author TEXT NOT NULL,
        author_kind TEXT NOT NULL DEFAULT 'user', author_thread_id TEXT, body TEXT NOT NULL,
        anchor_text TEXT, from_off INTEGER, to_off INTEGER, anchor_prefix TEXT, anchor_suffix TEXT,
        review INTEGER NOT NULL DEFAULT 0, resolved INTEGER NOT NULL DEFAULT 0, acked INTEGER NOT NULL DEFAULT 0,
        replacement TEXT, decision TEXT, decided_by TEXT, decided_by_thread_id TEXT, decided_at TEXT,
        decision_event_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )
"""
_TOKEN_PLAN = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"


def _columns(db: sqlite3.Connection) -> list[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(doc_comments)")]


def test_registered_as_the_current_step():
    assert SCHEMA_VERSION == 168
    assert MIGRATIONS[111] is migrate_111_to_112


def test_adds_nullable_region_columns_and_keeps_existing_rows():
    db = sqlite3.connect(":memory:")
    db.execute(_V111_DOC_COMMENTS)
    db.execute(
        "INSERT INTO doc_comments (id,artifact_id,author,body,created_at,updated_at) "
        "VALUES ('c1','a1','you','text comment','t','t')"
    )

    migrate_111_to_112(db)
    migrate_111_to_112(db)  # a second start changes nothing

    assert _columns(db)[-len(REGION_COLUMNS):] == list(REGION_COLUMNS)
    row = db.execute("SELECT body, region_number, region_x, image_rev, image_src FROM doc_comments").fetchone()
    assert row == ("text comment", None, None, None, None)


def test_one_number_per_image_is_enforced():
    db = sqlite3.connect(":memory:")
    db.execute(_V111_DOC_COMMENTS)
    migrate_111_to_112(db)
    insert = (
        "INSERT INTO doc_comments (id,artifact_id,author,body,created_at,updated_at,region_number,image_src) "
        "VALUES (?,?,?,?,?,?,?,?)"
    )
    db.execute(insert, ("c1", "a1", "you", "b", "t", "t", 1, None))
    db.execute(insert, ("c2", "a1", "you", "b", "t", "t", 1, "shots/one.png"))
    db.execute(insert, ("c3", "a2", "you", "b", "t", "t", 1, None))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ("c4", "a1", "you", "b", "t", "t", 1, None))


def test_fresh_schema_matches_a_migrated_one():
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(SCHEMA_SQL)
    migrated = sqlite3.connect(":memory:")
    migrated.execute(_V111_DOC_COMMENTS)
    migrate_111_to_112(migrated)
    migrate_133_to_134(migrated)  # the columns added since, which a fresh schema also lists last
    migrate_134_to_135(migrated)
    migrate_149_to_150(migrated)

    assert _columns(fresh) == _columns(migrated)
    index = "idx_doc_comments_region_number"
    assert fresh.execute("SELECT 1 FROM sqlite_master WHERE name=?", (index,)).fetchone()
    assert migrated.execute("SELECT 1 FROM sqlite_master WHERE name=?", (index,)).fetchone()


def test_a_database_without_doc_comments_passes():
    db = sqlite3.connect(":memory:")
    migrate_111_to_112(db)
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='doc_comments'").fetchone() is None


def test_a_v110_database_with_agent_use_terms_upgrades_through_v111_to_v112(isolated_db):
    import json

    from infra.database import get_db
    from infra.database_migrations import migrate_to_current
    from llm.endpoint_policy import endpoint_agent_use

    db = get_db()
    # A v110 database: the agent-use terms recorded by step 109, and no region columns yet.
    # Step 110 keeps the mark on a Token Plan host and clears it elsewhere; step 111 adds the regions.
    for sid, base in (("legacy", _TOKEN_PLAN), ("ordinary", "https://api.example.com/compatible-mode/v1")):
        db.execute(
            "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
            "VALUES (?,'external','external',?,'Alibaba Token Plan',443,'online','t','t','t',?)",
            (sid, sid, json.dumps({"base_url": base, "agent_use_restriction": "interactive_only"})),
        )
    db.execute("DROP TABLE doc_comments")
    db.execute(_V111_DOC_COMMENTS)
    db.execute(
        "INSERT INTO doc_comments (id,artifact_id,author,body,created_at,updated_at) VALUES ('c1','a1','you','text comment','t','t')"
    )
    db.execute("PRAGMA user_version=110")
    db.commit()

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert (result.migrated, result.from_version, result.to_version) == (True, 110, 168)
    assert _columns(db)[-len(REGION_COLUMNS) - 3:-3] == list(REGION_COLUMNS)  # then the step-134, 135 and 150 columns
    assert tuple(db.execute("SELECT body, region_number FROM doc_comments").fetchone()) == ("text comment", None)
    assert endpoint_agent_use("external@legacy/glm-5.3") == "interactive_only"
    assert endpoint_agent_use("external@ordinary/glm-5.3") == "allowed"
    assert not migrate_to_current(db, SCHEMA_VERSION).migrated
