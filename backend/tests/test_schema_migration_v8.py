"""Startup migration 7 -> 8 proof against a representative v7 database."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.plan_writer_authorizer import raw_plan_seed




def _v7_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            model TEXT,
            spawned_by TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE checkpoints (
            generation_order INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
            history_epoch INTEGER NOT NULL,
            through_message_id TEXT NOT NULL,
            trigger TEXT NOT NULL
        );
        CREATE TABLE plans (
            id TEXT PRIMARY KEY,
            owner_kind TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            parent_plan_id TEXT REFERENCES plans(id),
            state TEXT NOT NULL,
            revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            UNIQUE(owner_kind,owner_id)
        );
        CREATE TABLE plan_nodes (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
            parent_id TEXT REFERENCES plan_nodes(id) ON DELETE CASCADE,
            order_key INTEGER NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            acceptance_criteria TEXT NOT NULL,
            checkpoint_exception INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'pending',
            assignee TEXT,
            spawn_kind TEXT,
            owner_thread_id TEXT REFERENCES threads(id),
            spawned_thread_id TEXT REFERENCES threads(id),
            follow_up_of TEXT REFERENCES plan_nodes(id),
            watchdog_budget_seconds INTEGER,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT
        );
        CREATE TABLE plan_node_dependencies (
            node_id TEXT NOT NULL REFERENCES plan_nodes(id) ON DELETE CASCADE,
            depends_on_node_id TEXT NOT NULL REFERENCES plan_nodes(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY(node_id,depends_on_node_id)
        ) WITHOUT ROWID;
        CREATE TABLE plan_node_events (
            id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL REFERENCES plan_nodes(id),
            actor_id TEXT,
            provenance TEXT NOT NULL,
            event TEXT NOT NULL,
            prior_state TEXT,
            new_state TEXT,
            verification_result TEXT,
            causal_event_id TEXT REFERENCES plan_node_events(id),
            payload_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE artifact_identities (
            id TEXT PRIMARY KEY,
            integration TEXT NOT NULL,
            type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(integration,type,external_id)
        );
        CREATE TABLE plan_node_artifact_links (
            node_id TEXT NOT NULL REFERENCES plan_nodes(id) ON DELETE CASCADE,
            artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
            role TEXT NOT NULL,
            actor_id TEXT,
            provenance TEXT NOT NULL,
            source_event_id TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY(node_id,artifact_id,role)
        ) WITHOUT ROWID;
        CREATE TABLE expected_event_watches (
            id TEXT PRIMARY KEY,
            plan_id TEXT REFERENCES plans(id),
            plan_node_id TEXT REFERENCES plan_nodes(id),
            status TEXT NOT NULL,
            condition_type TEXT,
            condition_value TEXT,
            armed_at TEXT
        );
        CREATE TABLE active_turns (
            turn_order INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL REFERENCES threads(id),
            execution_epoch INTEGER NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            error TEXT,
            boot_epoch TEXT NOT NULL,
            retry_at TEXT
        );
        CREATE UNIQUE INDEX idx_active_turns_one_active
            ON active_turns(thread_id)
            WHERE status IN ('running','waiting_retry');
        CREATE TABLE llm_usage (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            turn_id TEXT,
            model TEXT NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            ttft_ms INTEGER,
            source TEXT NOT NULL DEFAULT 'conversation',
            created_at TEXT NOT NULL
        );
        PRAGMA user_version=7;
        """
    )
    for thread_id, parent in (
        ("owner", None),
        ("child", "owner"),
        ("other", None),
    ):
        db.execute(
            """INSERT INTO threads
               (id,user_id,model,spawned_by,created_at,updated_at)
               VALUES (?,'user','test/model',?,'t','t')""",
            (thread_id, parent),
        )
    for values in (
        ("p1", "thread", "owner", None),
        ("p2", "thread", "child", "p1"),
        ("p3", "thread", "other", None),
    ):
        db.execute(
            """INSERT INTO plans
               (id,owner_kind,owner_id,parent_plan_id,state,revision,created_at,updated_at)
               VALUES (?,?,?,?,'active',1,'t','t')""",
            values,
        )
    return db


def _node(
    db: sqlite3.Connection,
    node_id: str,
    plan_id: str,
    state: str,
    order_key: int,
    *,
    spawned_thread_id: str | None = None,
    follow_up_of: str | None = None,
    spawn_kind: str | None = None,
) -> None:
    with raw_plan_seed(db):
        db.execute(
            """INSERT INTO plan_nodes
               (id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,
                checkpoint_exception,state,spawned_thread_id,follow_up_of,
                spawn_kind,revision,created_at,updated_at)
               VALUES (?,?,NULL,?,'task',?,'verified',0,?,?,?,?,1,'t','t')""",
            (
                node_id,
                plan_id,
                order_key,
                node_id,
                state,
                spawned_thread_id,
                follow_up_of,
                spawn_kind,
            ),
        )


def _attach(db: sqlite3.Connection, node_id: str, child_plan_id: str, at: str) -> None:
    """Record the per-attachment event a real delegation always writes."""
    db.execute(
        """INSERT INTO plan_node_events
           (id,node_id,provenance,event,payload_json,created_at)
           VALUES (?,?,'test','spawned_thread_attached',?,?)""",
        (
            f"attach-{node_id}",
            node_id,
            json.dumps({"child_plan_id": child_plan_id}),
            at,
        ),
    )


def _seed_v7(db: sqlite3.Connection) -> None:
    _node(
        db, "root", "p1", "active", 0,
        spawned_thread_id="child", spawn_kind="delegated",
    )
    _attach(db, "root", "p2", "t")
    _node(db, "split-source", "p1", "active", 1)
    _node(db, "split-target", "p1", "active", 2)
    _node(db, "accepted-source", "p1", "accepted_with_followup", 3)
    _node(
        db,
        "accepted-follow",
        "p1",
        "pending",
        4,
        follow_up_of="accepted-source",
    )
    _node(db, "ordinary-source", "p1", "accepted", 5)
    _node(
        db,
        "ordinary-follow",
        "p1",
        "pending",
        6,
        follow_up_of="ordinary-source",
    )
    _node(db, "child-node", "p2", "active", 0)
    _node(db, "child-terminal", "p2", "accepted", 1)
    _node(db, "legacy-review", "p3", "review_requested", 0)
    db.execute(
        """INSERT INTO plan_node_events
           (id,node_id,provenance,event,payload_json,created_at)
           VALUES ('split-event','split-source','test','node_split',?,'t')""",
        (json.dumps({"split_node_id": "split-target"}),),
    )
    for values in (
        ("generic-blocker", "plan-node", "root"),
        ("real-decision", "plan-decision", "decision-1"),
    ):
        db.execute(
            """INSERT INTO artifact_identities
               (id,integration,type,external_id,state,metadata,created_at,updated_at)
               VALUES (?,'local',?,?,'active',?,'t','t')""",
            (*values, json.dumps({"attention_state": "raised"})),
        )
        db.execute(
            """INSERT INTO plan_node_artifact_links
               (node_id,artifact_id,role,provenance,created_at)
               VALUES ('root',?,'attention','test','t')""",
            (values[0],),
        )
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES ('turn-old','child',0,'test/model','completed','t','boot')"""
    )
    db.execute(
        """INSERT INTO llm_usage
           (id,thread_id,turn_id,model,total_tokens,cost_usd,source,created_at)
           VALUES ('usage-old','child','turn-old','test/model',12,0.5,'conversation','t')"""
    )
    db.commit()



def _migrate(path: Path):
    """Run the real ladder to 8, the way startup does."""
    from infra.database_migrations import migrate_to_current

    conn = sqlite3.connect(path)
    try:
        return migrate_to_current(conn, 8)
    finally:
        conn.close()


def _open(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def _shape(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
    assert row is not None, f"{name} missing"
    return "".join(row[0].split()).replace("IFNOTEXISTS", "").replace('"', "")


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    path = tmp_path / "corsia.db"
    db = _v7_db(path)
    try:
        _seed_v7(db)
    finally:
        db.close()
    return path


def test_migration_converts_plan_lineage_exactly(legacy_db: Path) -> None:
    """Commitments, attempts, review resets and retired attention, in one step."""
    result = _migrate(legacy_db)
    assert result.migrated and (result.from_version, result.to_version) == (7, 8)
    assert result.backup_path is not None and result.backup_path.exists()

    db = _open(legacy_db)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 8
        commitments = {
            row["id"]: row["commitment_id"]
            for row in db.execute("SELECT id,commitment_id FROM plan_nodes")
        }
        assert commitments["root"] == "root"
        assert commitments["split-target"] == "split-source"
        assert commitments["accepted-follow"] == "accepted-source"
        assert commitments["ordinary-follow"] == "ordinary-follow"
        assert commitments["child-node"] == "root"
        assert commitments["child-terminal"] == "child-terminal"
        assert commitments["legacy-review"] == "legacy-review"

        review = db.execute(
            "SELECT state,revision FROM plan_nodes WHERE id='legacy-review'"
        ).fetchone()
        assert tuple(review) == ("active", 2)
        event = db.execute(
            """SELECT prior_state,new_state,provenance FROM plan_node_events
               WHERE id='schema-v8-review-reset:legacy-review'"""
        ).fetchone()
        assert tuple(event) == ("review_requested", "active", "schema_v8_migration")

        attempts = {
            row["node_id"]: row["executor_thread_id"]
            for row in db.execute(
                "SELECT node_id,executor_thread_id FROM plan_node_attempts "
                "WHERE released_at IS NULL"
            )
        }
        assert attempts == {"child-node": "child", "legacy-review": "other"}

        turn = db.execute(
            """SELECT id,plan_id,plan_node_id,plan_attempt_id,
                      plan_commitment_id,plan_usage_role FROM active_turns"""
        ).fetchone()
        assert tuple(turn) == ("turn-old", None, None, None, None, None)
        usage = db.execute(
            "SELECT total_tokens,cost_usd,plan_commitment_id FROM llm_usage "
            "WHERE id='usage-old'"
        ).fetchone()
        assert tuple(usage) == (12, 0.5, None)

        generic = db.execute(
            "SELECT state,metadata FROM artifact_identities WHERE id='generic-blocker'"
        ).fetchone()
        assert generic["state"] == "archived"
        assert json.loads(generic["metadata"])["attention_reason"] == (
            "schema_v8_canonical_plan_projection"
        )
        assert db.execute(
            "SELECT state FROM artifact_identities WHERE id='real-decision'"
        ).fetchone()["state"] == "active"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


def test_migration_is_idempotent_at_current(legacy_db: Path) -> None:
    _migrate(legacy_db)
    again = _migrate(legacy_db)
    assert not again.migrated
    db = _open(legacy_db)
    try:
        assert db.execute("SELECT COUNT(*) FROM plan_node_attempts").fetchone()[0] == 2
    finally:
        db.close()


def test_migrated_plan_tables_match_a_fresh_schema(legacy_db: Path) -> None:
    """The invariant the version bump exists to protect.

    The step builds its tables from the live schema, so a later edit to
    ``infra.plan_schema`` without a new step is caught here, not in production.
    """
    from infra.schema import SCHEMA_SQL

    _migrate(legacy_db)
    fresh = sqlite3.connect(":memory:")
    migrated = _open(legacy_db)
    try:
        fresh.executescript(SCHEMA_SQL)
        names = [
            row[0]
            for row in fresh.execute(
                """SELECT name FROM sqlite_master
                   WHERE sql IS NOT NULL AND tbl_name IN (
                     'plan_nodes','active_turns','llm_usage',
                     'plan_node_attempts','plan_tool_dispatches','plan_node_holds')"""
            )
        ]
        assert names
        for name in names:
            assert _shape(migrated, name) == _shape(fresh, name), name
    finally:
        fresh.close()
        migrated.close()


@pytest.fixture
def reused_delegate_db(tmp_path: Path) -> Path:
    """One delegate thread reused across three parent tasks: the prod shape.

    Child plan 19abe593 (thread ecabc96e) was attached to three accepted nodes
    of one parent plan, ~30 minutes apart, in clean 1:1 sequential order. That
    is ordinary reuse of a worker thread, and it aborted the release.
    """
    path = tmp_path / "corsia.db"
    db = _v7_db(path)
    try:
        _seed_v7(db)
        db.execute(
            """INSERT INTO threads (id,user_id,model,spawned_by,created_at,updated_at)
               VALUES ('reused','user','test/model','owner','t','t')"""
        )
        db.execute(
            """INSERT INTO plans
               (id,owner_kind,owner_id,parent_plan_id,state,revision,
                created_at,updated_at)
               VALUES ('p4','thread','reused','p1','active',1,'t','t')"""
        )
        for index, node_id in enumerate(("deleg-1", "deleg-2", "deleg-3")):
            _node(
                db, node_id, "p1", "accepted", 10 + index,
                spawned_thread_id="reused", spawn_kind="delegated",
            )
            _attach(db, node_id, "p4", f"t{index}")
        _node(db, "reused-work", "p4", "accepted", 0)
        db.commit()
    finally:
        db.close()
    return path


def test_a_delegate_thread_reused_across_tasks_migrates(reused_delegate_db: Path) -> None:
    """The defect that took production down, as its own case.

    The first version of this migration matched parent tasks on
    `spawned_thread_id` alone, so three sequential delegations to one thread
    looked like three simultaneous attachments to one child plan, and it
    raised. Reusing a delegate thread is ordinary, so that was normal data.
    """
    result = _migrate(reused_delegate_db)

    assert result.migrated and (result.from_version, result.to_version) == (7, 8)
    db = _open(reused_delegate_db)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 8
        commitments = {
            row["id"]: row["commitment_id"]
            for row in db.execute("SELECT id,commitment_id FROM plan_nodes")
        }
        # Each settled delegation keeps its own commitment: none is open, so
        # none inherits, which is exactly why aborting on them was gratuitous.
        for node_id in ("deleg-1", "deleg-2", "deleg-3"):
            assert commitments[node_id] == node_id
        assert commitments["reused-work"] == "reused-work"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


def test_a_live_attachment_still_carries_the_parent_commitment(
    reused_delegate_db: Path,
) -> None:
    """Accepting reuse must not cost the inheritance the migration exists for.

    Distinguishing "several closed attachments" from "one live attachment" is
    the whole point of the predicate, so prove the live case still resolves
    while the reused closed ones sit beside it.
    """
    db = sqlite3.connect(reused_delegate_db)
    db.row_factory = sqlite3.Row
    try:
        _node(
            db, "deleg-live", "p1", "active", 20,
            spawned_thread_id="reused", spawn_kind="delegated",
        )
        _attach(db, "deleg-live", "p4", "t9")
        _node(db, "reused-open", "p4", "active", 1)
        db.commit()
    finally:
        db.close()

    _migrate(reused_delegate_db)

    db = _open(reused_delegate_db)
    try:
        commitments = {
            row["id"]: row["commitment_id"]
            for row in db.execute("SELECT id,commitment_id FROM plan_nodes")
        }
        assert commitments["reused-open"] == "deleg-live"
        assert commitments["reused-work"] == "reused-work"
    finally:
        db.close()


def test_attachment_resolves_against_a_v7_database(tmp_path: Path) -> None:
    """The contract the frozen 7->8 migration consumes from live runtime code.

    ``migration_7_to_8_lineage`` asks ``plans.commitments`` which parent task
    owns a child plan instead of carrying a second copy of that rule. The
    price of one definition is that the live rule must stay runnable against a
    database still at v7: a change reading a later column would leave every
    existing v7 database unable to migrate at all.

    The ladder tests above would also fail if that happened, but they would
    fail as a migration error. Calling the function directly, at v7, is what
    makes the break name its own cause for whoever edits ``commitments.py``.
    """
    from plans import commitments as plan_commitments

    db = _v7_db(tmp_path / "v7.db")
    try:
        _node(
            db, "root", "p1", "active", 0,
            spawned_thread_id="child", spawn_kind="delegated",
        )
        _attach(db, "root", "p2", "t")
        db.commit()

        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        node = plan_commitments.attached_parent_node("p2", db=db)
        assert node is not None
        assert node["id"] == "root"
    finally:
        db.close()
