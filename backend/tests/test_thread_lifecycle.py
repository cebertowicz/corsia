"""Phase 3b — thread lifecycle transitions + retention.

Locks each state transition, the tool-I/O + working/ trims, the
summary-survives-thread-deletion invariant, and idempotency (re-run = no-op).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.thread_lifecycle as lifecycle
import core.thread_lifecycle_trims as lifecycle_trims
from core.thread_lifecycle import (
    LifecycleConfig,
    ThreadLifecycleError,
    run_retention_sweep,
)
from core.thread_lifecycle import set_user_archive_state as _set_user_archive_state
from infra import write_admission
from infra.database import write_transaction
from infra.schema import (
    ARTIFACT_IDENTITY_SCHEMA_SQL,
    STRUCTURED_PLAN_SCHEMA_SQL,
)
from infra.checkpoint_schema import CHECKPOINT_SCHEMA_SQL
from infra.continuity_schema import CONTINUITY_SCHEMA_SQL
from infra.continuity_staging import STAGING_SCHEMA_SQL
from infra.presence_schema import PRESENCE_SCHEMA_SQL
from infra.thread_updates_schema import THREAD_UPDATES_SCHEMA_SQL

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
CFG = LifecycleConfig(
    dormant_after_days=14,
    archive_after_days=30,
    purge_after_days=180,
    tool_trim_after_days=30,
    working_ttl_days=7,
)


def test_lifecycle_helper_uses_thread_terminology() -> None:
    assert hasattr(lifecycle, "_evaluate_thread")
    assert not hasattr(lifecycle, "_evaluate_session")


def _db(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path) if path is not None else ":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            lifecycle_state TEXT DEFAULT 'active',
            dormant_at TEXT,
            archived_at TEXT,
            archived_reason TEXT,
            purged_at TEXT,
            pinned INTEGER DEFAULT 0,
            agent_enabled INTEGER DEFAULT 0,
            spawned_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            history_epoch INTEGER NOT NULL DEFAULT 0,
            runtime_context_epoch INTEGER NOT NULL DEFAULT 0,
            next_message_seq INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE lesson_placement_checks (
            thread_id TEXT NOT NULL,
            history_epoch INTEGER NOT NULL,
            entry_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            check_name TEXT NOT NULL,
            flag TEXT NOT NULL,
            override_reason TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (thread_id, history_epoch, entry_id, revision, check_name, flag)
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'delivered',
            delivering_reason TEXT,
            delivery_blocked_since TEXT,
            next_attempt_at TEXT,
            seq INTEGER
        );
        CREATE TABLE thread_inbox (
            thread_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            queue_order INTEGER NOT NULL,
            created_at TEXT
        );
        -- The rest of the thread's owned delivery state. Purge destroys all of
        -- it through one reactor door, so this schema has to carry all of it.
        CREATE TABLE message_delivery_contracts (
            message_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            delivery_intent TEXT NOT NULL,
            descriptor_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE active_turns (
            turn_order INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            started_at TEXT NOT NULL,
            usage_receipts_json TEXT NOT NULL DEFAULT '{}',
            idle_checkpoint_pending INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX idx_messages_session_seq
            ON messages(thread_id, seq);
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT);

        CREATE TABLE artifact_identities (
            id TEXT PRIMARY KEY,
            integration TEXT NOT NULL,
            type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT,
            description TEXT,
            path TEXT,
            state TEXT NOT NULL DEFAULT 'active',
            retention TEXT NOT NULL DEFAULT 'durable'
                CHECK (retention IN ('ephemeral', 'durable')),
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(integration, type, external_id)
        );
        CREATE TABLE artifact_registrations (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
            parent_thread_id TEXT NOT NULL,
            relationship_state TEXT NOT NULL DEFAULT 'owner-active',
            surfacing TEXT NOT NULL DEFAULT 'visible'
                CHECK (surfacing IN ('visible', 'quiet')),
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(artifact_id, parent_thread_id)
        );
        CREATE TABLE artifact_registration_events (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor_thread_id TEXT,
            authority TEXT,
            reason TEXT,
            source_path TEXT,
            causal_event_id TEXT,
            affected_thread_id TEXT,
            old_owner_thread_id TEXT,
            new_owner_thread_id TEXT,
            old_relationship_state TEXT,
            new_relationship_state TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.executescript(CHECKPOINT_SCHEMA_SQL)
    conn.executescript(CONTINUITY_SCHEMA_SQL)
    conn.executescript(STAGING_SCHEMA_SQL)
    conn.executescript(PRESENCE_SCHEMA_SQL)
    conn.executescript(THREAD_UPDATES_SCHEMA_SQL)
    # Archive/purge now carries a plan-decision consequence too, so the
    # fixture loads those real schemas rather than a hand-copied subset.
    conn.executescript(ARTIFACT_IDENTITY_SCHEMA_SQL)
    conn.executescript(STRUCTURED_PLAN_SCHEMA_SQL)
    return conn


def _peer_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def set_user_archive_state(db, *args, **kwargs):
    """The archive transition under the write scope its callers own."""
    db.commit()
    with write_transaction(db, scope="archive_state"):
        return _set_user_archive_state(db, *args, **kwargs)


class _CommitBeforeFirstCandidateLock:
    """Commit a competing writer after the sweep snapshot, before validation.

    The racer commits just before the sweep's first write admission: FIFO
    admission lets a writer in this process land only then, and at BEGIN
    IMMEDIATE it would be a refused second outermost write on this thread.
    """

    def __init__(self, db, action, monkeypatch):
        self._db = db
        self._action = action
        self.fired = False
        admit = write_admission.admit

        def admit_after_racer(connection, scope):
            if connection is self and not self.fired:
                self.fired = True
                self._action()
            return admit(connection, scope)

        monkeypatch.setattr(write_admission, "admit", admit_after_racer)

    def __getattr__(self, name):
        return getattr(self._db, name)


class _TrackToolTrimBatches:
    """Record how many payload updates each tool-trim writer owns."""

    def __init__(self, db):
        self._db = db
        self.batch_update_counts: list[int] = []

    def execute(self, sql, params=()):
        if sql == "BEGIN IMMEDIATE":
            self.batch_update_counts.append(0)
        elif sql.startswith("UPDATE messages AS m SET"):
            self.batch_update_counts[-1] += 1
        return self._db.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._db, name)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _add_session(db, sid, *, state="active", last_activity=None, created=None,
                 dormant_at=None, archived_at=None, archived_reason=None,
                 pinned=0, agent_enabled=0, spawned_by=None):
    created = created or (NOW - timedelta(days=400))
    db.execute(
        "INSERT INTO threads (id, lifecycle_state, dormant_at, archived_at, "
        "archived_reason, pinned, agent_enabled, spawned_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, state,
         _iso(dormant_at) if dormant_at else None,
         _iso(archived_at) if archived_at else None,
         archived_reason, pinned, agent_enabled, spawned_by,
         _iso(created), _iso(created)),
    )
    if last_activity is not None:
        db.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at) "
            "VALUES (?,?,?,?,?)",
            (f"{sid}-m", sid, "user", "hi", _iso(last_activity)),
        )
    db.commit()


def _add_artifact_identity(
    db,
    artifact_id: str,
    *,
    path: Path | None = None,
    retention: str = "durable",
) -> None:
    timestamp = _iso(NOW - timedelta(days=1))
    db.execute(
        "INSERT INTO artifact_identities "
        "(id,integration,type,external_id,path,state,retention,created_at,updated_at) "
        "VALUES (?,'workspace','document',?,?,?,?,?,?)",
        (
            artifact_id,
            f"workspace://test/{artifact_id}",
            str(path) if path is not None else None,
            "active",
            retention,
            timestamp,
            timestamp,
        ),
    )


def _add_artifact_registration(
    db,
    artifact_id: str,
    parent_thread_id: str,
    *,
    relationship_state: str = "owner-active",
) -> None:
    timestamp = _iso(NOW - timedelta(days=1))
    db.execute(
        "INSERT INTO artifact_registrations "
        "(id,artifact_id,parent_thread_id,relationship_state,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (
            f"{artifact_id}-{parent_thread_id}",
            artifact_id,
            parent_thread_id,
            relationship_state,
            timestamp,
            timestamp,
        ),
    )


def _thread_root(ws: Path, sid: str) -> Path:
    root = ws / ".threads" / sid
    (root / "working").mkdir(parents=True, exist_ok=True)
    (root / "context").mkdir(parents=True, exist_ok=True)
    (root / "skills").mkdir(parents=True, exist_ok=True)
    return root


def _seed_covering_checkpoint(
    db: sqlite3.Connection,
    sid: str,
    *,
    through_seq: int | None = None,
    history_epoch: int | None = None,
) -> None:
    """Seed one installed checkpoint over the fixture's current messages."""
    db.execute(
        "UPDATE messages SET seq = rowid "
        "WHERE thread_id = ? AND seq IS NULL",
        (sid,),
    )
    latest = db.execute(
        "SELECT id, seq FROM messages WHERE thread_id = ? "
        "AND seq IS NOT NULL ORDER BY seq DESC LIMIT 1",
        (sid,),
    ).fetchone()
    assert latest is not None
    boundary_seq = through_seq or int(latest["seq"])
    boundary = db.execute(
        "SELECT id FROM messages WHERE thread_id = ? AND seq = ?",
        (sid, boundary_seq),
    ).fetchone()
    assert boundary is not None
    current_epoch = db.execute(
        "SELECT history_epoch FROM threads WHERE id = ?",
        (sid,),
    ).fetchone()[0]
    epoch = current_epoch if history_epoch is None else history_epoch
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
        "status,source_harness,source_model,writer_harness,writer_model,"
        "continuity_base_revision,continuity_base_snapshot_json,raw_result,"
        "started_at,captured_at,installed_at,ended_at) "
        "VALUES (?,?,?,?,?,'pressure','warm','installed','native','test-model',"
        "'native','test-model',0,'[]','retained checkpoint',?,?,?,?)",
        (
            f"{sid}-checkpoint-{epoch}-{boundary_seq}",
            sid,
            epoch,
            boundary_seq,
            boundary["id"],
            _iso(NOW),
            _iso(NOW),
            _iso(NOW),
            _iso(NOW),
        ),
    )
    db.commit()


# --- transitions ------------------------------------------------------------

def test_active_to_dormant_after_idle(tmp_path):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=15))
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert report.dormant == ["s1"]
    row = db.execute("SELECT lifecycle_state, dormant_at FROM threads WHERE id='s1'").fetchone()
    assert row["lifecycle_state"] == "dormant"
    assert row["dormant_at"] is not None


def test_active_not_dormant_before_threshold(tmp_path):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=13))
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert report.dormant == []
    assert db.execute("SELECT lifecycle_state FROM threads WHERE id='s1'").fetchone()[0] == "active"


def test_dormant_to_archived_keeps_working_context_and_skills(tmp_path):
    db = _db()
    _add_session(db, "s1", state="dormant",
                 dormant_at=NOW - timedelta(days=31),
                 last_activity=NOW - timedelta(days=45))
    root = _thread_root(tmp_path, "s1")
    (root / "working" / "scratch.txt").write_text("tmp")
    (root / "context" / "boot.md").write_text("durable")
    (root / "skills" / "s" / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
    (root / "skills" / "keep.txt").write_text("skill")

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.archived == ["s1"]
    row = db.execute("SELECT lifecycle_state, archived_at FROM threads WHERE id='s1'").fetchone()
    assert row["lifecycle_state"] == "archived"
    assert row["archived_at"] is not None
    assert (root / "working" / "scratch.txt").read_text() == "tmp"
    assert (root / "context" / "boot.md").exists()
    assert (root / "skills" / "keep.txt").exists()


def test_user_archive_is_synchronous_and_sweep_keeps_working(tmp_path):
    db = _db()
    _add_session(
        db,
        "s1",
        state="active",
        archived_at=NOW - timedelta(days=100),
        last_activity=NOW - timedelta(days=1),
    )
    root = _thread_root(tmp_path, "s1")
    (root / "working" / "x.txt").write_text("tmp")

    assert set_user_archive_state(db, "s1", archived=True, now=NOW) is True
    row = db.execute(
        "SELECT lifecycle_state, archived_at, archived_reason FROM threads WHERE id='s1'"
    ).fetchone()
    assert tuple(row) == ("archived", _iso(NOW), "user")
    assert (root / "working" / "x.txt").exists()
    db.commit()

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert report.archived == []
    assert (root / "working" / "x.txt").read_text() == "tmp"


def test_archived_to_purged_removes_dir_and_history(tmp_path):
    db = _db()
    _add_session(db, "s1", state="archived", archived_reason="dormancy",
                 archived_at=NOW - timedelta(days=181),
                 last_activity=NOW - timedelta(days=250))
    db.execute(
        "INSERT INTO messages (id, thread_id, role, content, created_at) VALUES (?,?,?,?,?)",
        ("m-old", "s1", "assistant", "history", _iso(NOW - timedelta(days=250))),
    )
    db.commit()
    _seed_covering_checkpoint(db, "s1")
    root = _thread_root(tmp_path, "s1")
    (root / "context" / "boot.md").write_text("durable")

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.purged == ["s1"]
    assert not root.exists()
    assert db.execute(
        "SELECT lifecycle_state FROM threads WHERE id='s1'"
    ).fetchone()[0] == "purged"
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='s1'"
    ).fetchone()[0] == 0
    row = db.execute(
        "SELECT lifecycle_state, purged_at, history_epoch "
        "FROM threads WHERE id='s1'"
    ).fetchone()
    assert row["lifecycle_state"] == "purged"
    assert row["purged_at"] is not None
    assert row["history_epoch"] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id='s1'"
    ).fetchone()[0] == 0

    rerun = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert rerun.purged == []
    assert db.execute(
        "SELECT history_epoch FROM threads WHERE id='s1'"
    ).fetchone()[0] == 1


def test_failed_physical_purge_stays_purged_and_same_sweep_finishes_cleanup(tmp_path, monkeypatch):
    db = _db()
    _add_session(
        db,
        "s1",
        state="archived",
        archived_reason="dormancy",
        archived_at=NOW - timedelta(days=181),
        last_activity=NOW - timedelta(days=250),
    )
    db.execute(
        "INSERT INTO messages (id, thread_id, role, content, created_at) "
        "VALUES (?,?,?,?,?)",
        ("m-old", "s1", "assistant", "history", _iso(NOW - timedelta(days=250))),
    )
    db.commit()
    root = _thread_root(tmp_path, "s1")
    real_rmtree = lifecycle.shutil.rmtree
    monkeypatch.setattr(lifecycle.shutil, "rmtree", lambda *args, **kwargs: None)

    first = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert first.purged == ["s1"]
    assert first.purge_cleanup_errors == 1
    assert first.as_dict()["purge_cleanup_errors"] == 1
    assert root.exists()
    assert db.execute(
        "SELECT lifecycle_state FROM threads WHERE id='s1'"
    ).fetchone()[0] == "purged"
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='s1'"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT history_epoch FROM threads WHERE id='s1'"
    ).fetchone()[0] == 1
    with pytest.raises(ThreadLifecycleError, match="purged"):
        set_user_archive_state(db, "s1", archived=False, now=NOW)
    db.rollback()

    monkeypatch.setattr(lifecycle.shutil, "rmtree", real_rmtree)
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.purged == []  # Cleanup is not a second lifecycle transition.
    assert not root.exists()
    assert db.execute(
        "SELECT lifecycle_state FROM threads WHERE id='s1'"
    ).fetchone()[0] == "purged"
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='s1'"
    ).fetchone()[0] == 0


def test_one_undeletable_purged_tree_does_not_stop_the_sweep(tmp_path, monkeypatch):
    db = _db()
    for sid in ("s1", "s2"):
        _add_session(
            db,
            sid,
            state="archived",
            archived_reason="dormancy",
            archived_at=NOW - timedelta(days=181),
            last_activity=NOW - timedelta(days=250),
        )
    db.commit()
    stuck, freed = _thread_root(tmp_path, "s1"), _thread_root(tmp_path, "s2")
    real_rmtree = lifecycle.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path) != stuck:
            real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(lifecycle.shutil, "rmtree", rmtree)

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.purged == ["s1", "s2"]
    assert report.purge_cleanup_errors == 1
    assert stuck.exists()
    assert not freed.exists()


def test_purge_database_failure_rolls_back_checkpoint_and_messages(tmp_path):
    db = _db()
    _add_session(
        db,
        "s1",
        state="archived",
        archived_reason="dormancy",
        archived_at=NOW - timedelta(days=181),
        last_activity=NOW - timedelta(days=250),
    )
    db.execute(
        "INSERT INTO messages (id, thread_id, role, content, created_at) "
        "VALUES (?,?,?,?,?)",
        ("m-old", "s1", "assistant", "history", _iso(NOW - timedelta(days=250))),
    )
    db.commit()
    _seed_covering_checkpoint(db, "s1")
    root = _thread_root(tmp_path, "s1")
    (root / "working" / "keep.txt").write_text("SQL failure must preserve files")
    db.executescript(
        """
        CREATE TRIGGER abort_purge_epoch
        BEFORE UPDATE OF history_epoch ON threads
        WHEN new.id = 's1'
        BEGIN
            SELECT RAISE(ABORT, 'injected purge failure');
        END;
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected purge failure"):
        run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert db.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id='s1'"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='s1'"
    ).fetchone()[0] == 2
    assert db.execute(
        "SELECT history_epoch FROM threads WHERE id='s1'"
    ).fetchone()[0] == 0
    assert (root / "working" / "keep.txt").read_text() == "SQL failure must preserve files"


def test_archive_never_attempts_physical_cleanup(
    tmp_path,
    monkeypatch,
):
    db = _db()
    _add_session(
        db,
        "s1",
        state="dormant",
        dormant_at=NOW - timedelta(days=31),
        last_activity=NOW - timedelta(days=45),
    )
    root = _thread_root(tmp_path, "s1")
    (root / "working" / "scratch.txt").write_text("keep until cleanup succeeds")
    def reject_cleanup(*args, **kwargs):
        raise AssertionError("archive must not remove files")

    monkeypatch.setattr(lifecycle.shutil, "rmtree", reject_cleanup)
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    row = db.execute(
        "SELECT lifecycle_state, archived_at, archived_reason "
        "FROM threads WHERE id='s1'"
    ).fetchone()
    assert tuple(row) == ("archived", _iso(NOW), "dormancy")
    assert report.archived == ["s1"]
    assert (root / "working" / "scratch.txt").exists()

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.archived == []
    assert (root / "working" / "scratch.txt").exists()
    row = db.execute(
        "SELECT lifecycle_state, archived_reason FROM threads WHERE id='s1'"
    ).fetchone()
    assert tuple(row) == ("archived", "dormancy")


def test_archive_preserves_owned_and_shared_files_and_identities(
    tmp_path,
    monkeypatch,
):
    db = _db()
    _add_session(
        db,
        "s1",
        state="dormant",
        dormant_at=NOW - timedelta(days=31),
        last_activity=NOW - timedelta(days=45),
    )
    root = _thread_root(tmp_path, "s1")
    owned_tmp = root / "working" / "owned-tmp.txt"
    owned_durable = root / "working" / "owned-durable.txt"
    shared = root / "working" / "shared.txt"
    for path in (owned_tmp, owned_durable, shared):
        path.write_text(path.name)

    _add_artifact_identity(db, "owned-tmp", path=owned_tmp, retention="ephemeral")
    _add_artifact_registration(db, "owned-tmp", "s1")
    _add_artifact_identity(db, "owned-durable", path=owned_durable, retention="durable")
    _add_artifact_registration(db, "owned-durable", "s1")
    _add_artifact_identity(db, "shared", path=shared, retention="ephemeral")
    _add_artifact_registration(db, "shared", "other-owner")
    _add_artifact_registration(
        db,
        "shared",
        "s1",
        relationship_state="subscriber-recent",
    )
    db.commit()

    def reject_cleanup(*args, **kwargs):
        raise AssertionError("archive must not remove files")

    monkeypatch.setattr(lifecycle.shutil, "rmtree", reject_cleanup)
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert report.archived == ["s1"]

    assert db.execute(
        "SELECT lifecycle_state FROM threads WHERE id='s1'"
    ).fetchone()[0] == "archived"
    assert {
        row["id"]: row["state"]
        for row in db.execute(
            "SELECT id,state FROM artifact_identities ORDER BY id"
        ).fetchall()
    } == {
        "owned-durable": "active",
        "owned-tmp": "active",
        "shared": "active",
    }

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.archived == []
    assert all(path.read_text() == path.name for path in (owned_tmp, owned_durable, shared))
    assert {
        row["id"]: row["state"]
        for row in db.execute(
            "SELECT id,state FROM artifact_identities ORDER BY id"
        ).fetchall()
    } == {
        "owned-durable": "active",
        "owned-tmp": "active",
        "shared": "active",
    }


def test_pinned_archived_thread_never_purged(tmp_path):
    db = _db()
    _add_session(db, "s1", state="archived", pinned=1, archived_reason="dormancy",
                 archived_at=NOW - timedelta(days=999),
                 last_activity=NOW - timedelta(days=999))
    _thread_root(tmp_path, "s1")
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert report.purged == []
    assert (tmp_path / ".threads" / "s1").exists()
    assert db.execute("SELECT lifecycle_state FROM threads WHERE id='s1'").fetchone()[0] == "archived"


def test_agent_thread_excluded_from_transitions(tmp_path):
    db = _db()
    _add_session(db, "agent", agent_enabled=1,
                 last_activity=NOW - timedelta(days=400))
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert report.dormant == [] and report.archived == [] and report.purged == []
    assert db.execute("SELECT lifecycle_state FROM threads WHERE id='agent'").fetchone()[0] == "active"


def test_user_archived_agent_keeps_files_without_retention_transition(
    tmp_path,
):
    db = _db()
    _add_session(
        db,
        "agent",
        state="archived",
        archived_reason="user",
        archived_at=NOW,
        agent_enabled=1,
        last_activity=NOW,
    )
    root = _thread_root(tmp_path, "agent")
    (root / "working" / "scratch.txt").write_text("discard")

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.reactivated == []
    assert report.dormant == []
    assert report.archived == []
    assert report.purged == []
    assert (root / "working" / "scratch.txt").read_text() == "discard"
    assert db.execute(
        "SELECT lifecycle_state FROM threads WHERE id='agent'"
    ).fetchone()[0] == "archived"


def test_unarchived_old_thread_reactivates_and_is_not_purged(tmp_path):
    # User unarchive is synchronous and takes the thread off the purge track,
    # even when it is old and has no new activity.
    db = _db()
    _add_session(db, "s1", state="archived", archived_reason="user",
                 archived_at=NOW - timedelta(days=999),
                 last_activity=NOW - timedelta(days=999))
    root = _thread_root(tmp_path, "s1")
    (root / "context" / "boot.md").write_text("durable")

    assert set_user_archive_state(db, "s1", archived=False, now=NOW) is True
    immediate = db.execute(
        "SELECT lifecycle_state, archived_at, archived_reason FROM threads WHERE id='s1'"
    ).fetchone()
    assert tuple(immediate) == ("active", None, None)
    db.commit()

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.purged == []
    assert report.reactivated == []
    assert root.exists()
    row = db.execute(
        "SELECT lifecycle_state, archived_at, archived_reason FROM threads WHERE id='s1'"
    ).fetchone()
    # Reactivated; since it's still idle it settles into dormant this sweep, but
    # NOT archived/purged and off the purge clock (archived_at cleared).
    assert row["lifecycle_state"] in ("active", "dormant")
    assert row["archived_at"] is None
    assert row["archived_reason"] is None


def test_dormancy_archived_idle_thread_is_not_reactivated_and_purges(tmp_path):
    # The mirror: a NEVER-touched dormancy archive (reason='dormancy', archived
    # flag never set) must NOT be reactivated by the un-archive path — otherwise
    # dormancy-purge is silently defeated. Past 180d archived -> it purges.
    db = _db()
    _add_session(db, "s1", state="archived", archived_reason="dormancy",
                 archived_at=NOW - timedelta(days=181),
                 last_activity=NOW - timedelta(days=250))
    _thread_root(tmp_path, "s1")
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert report.reactivated == []
    assert report.purged == ["s1"]
    assert not (tmp_path / ".threads" / "s1").exists()


def test_a_settled_worker_is_not_reactivated_by_recent_activity(tmp_path):
    # A worker archived because its assignment settled was retired on purpose;
    # only an explicit new assignment brings it back, never the sweep.
    db = _db()
    _add_session(db, "w1", state="archived", archived_reason="assignment_settled",
                 archived_at=NOW - timedelta(days=1),
                 last_activity=NOW - timedelta(hours=2))
    _thread_root(tmp_path, "w1")
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert report.reactivated == []
    row = db.execute(
        "SELECT lifecycle_state, archived_reason FROM threads WHERE id='w1'"
    ).fetchone()
    assert tuple(row) == ("archived", "assignment_settled")


def test_user_archive_of_dormancy_archived_thread_upgrades_provenance(tmp_path):
    # A user archive changes provenance without resetting the retention clock;
    # unarchive then reactivates synchronously.
    db = _db()
    _add_session(db, "s1", state="archived", archived_reason="dormancy",
                 archived_at=NOW - timedelta(days=100),
                 last_activity=NOW - timedelta(days=250))
    _thread_root(tmp_path, "s1")
    assert set_user_archive_state(db, "s1", archived=True, now=NOW) is True
    row = db.execute(
        "SELECT archived_at, archived_reason FROM threads WHERE id='s1'"
    ).fetchone()
    assert tuple(row) == (_iso(NOW - timedelta(days=100)), "user")
    assert set_user_archive_state(db, "s1", archived=False, now=NOW) is True
    assert db.execute("SELECT lifecycle_state FROM threads WHERE id='s1'").fetchone()[0] == "active"
    assert (tmp_path / ".threads" / "s1").exists()


def test_unarchive_dormancy_archive_is_a_noop():
    db = _db()
    _add_session(
        db,
        "s1",
        state="archived",
        archived_reason="dormancy",
        archived_at=NOW - timedelta(days=10),
    )
    assert set_user_archive_state(db, "s1", archived=False, now=NOW) is False
    row = db.execute(
        "SELECT lifecycle_state, archived_reason FROM threads WHERE id='s1'"
    ).fetchone()
    assert tuple(row) == ("archived", "dormancy")


def test_user_archive_change_rejects_purged_thread():
    db = _db()
    _add_session(db, "s1", state="purged")
    with pytest.raises(ThreadLifecycleError, match="purged"):
        set_user_archive_state(db, "s1", archived=True, now=NOW)


@pytest.mark.parametrize(
    ("state", "reason", "archived", "update_prefix"),
    [
        ("active", None, True, "UPDATE threads SET lifecycle_state = 'archived'"),
        ("archived", "user", False, "UPDATE threads SET lifecycle_state = 'active'"),
    ],
)
def test_user_archive_transition_cannot_overwrite_concurrent_purge(
    tmp_path,
    state,
    reason,
    archived,
    update_prefix,
):
    path = tmp_path / "purge-race.sqlite"
    db = _db(path)
    purger_db = _peer_db(path)
    _add_session(
        db,
        "s1",
        state=state,
        archived_reason=reason,
        archived_at=NOW - timedelta(days=181) if reason else None,
    )
    purged_at = _iso(NOW - timedelta(seconds=1))

    class PurgeBeforeArchiveUpdate:
        fired = False

        def execute(self, sql, params=()):
            if not self.fired and sql.startswith(update_prefix):
                self.fired = True
                purger_db.execute(
                    "UPDATE threads SET lifecycle_state='purged', purged_at=? "
                    "WHERE id='s1'",
                    (purged_at,),
                )
                purger_db.commit()
            return db.execute(sql, params)

    with pytest.raises(ThreadLifecycleError, match="purged"):
        _set_user_archive_state(
            PurgeBeforeArchiveUpdate(),
            "s1",
            archived=archived,
            now=NOW,
        )

    row = db.execute(
        "SELECT lifecycle_state,purged_at,archived_reason FROM threads WHERE id='s1'"
    ).fetchone()
    assert tuple(row) == ("purged", purged_at, reason)


def test_sweep_revalidates_user_archive_committed_after_candidate_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "archive-race.sqlite"
    sweep_db = _db(path)
    actor_db = _peer_db(path)
    _add_session(
        sweep_db,
        "s1",
        state="active",
        last_activity=NOW - timedelta(days=20),
    )
    root = _thread_root(tmp_path, "s1")
    (root / "working" / "scratch.txt").write_text("discard")

    def commit_archive():
        assert set_user_archive_state(
            actor_db,
            "s1",
            archived=True,
            now=NOW - timedelta(seconds=1),
        ) is True
        actor_db.commit()

    interleaved = _CommitBeforeFirstCandidateLock(sweep_db, commit_archive, monkeypatch)
    report = run_retention_sweep(
        interleaved,
        now=NOW,
        config=CFG,
        workspace=tmp_path,
    )

    assert interleaved.fired is True
    assert report.dormant == []
    row = sweep_db.execute(
        "SELECT lifecycle_state, archived_reason FROM threads WHERE id='s1'"
    ).fetchone()
    assert tuple(row) == ("archived", "user")
    assert (root / "working" / "scratch.txt").read_text() == "discard"


def test_sweep_revalidates_user_unarchive_before_purging(tmp_path, monkeypatch):
    path = tmp_path / "unarchive-race.sqlite"
    sweep_db = _db(path)
    actor_db = _peer_db(path)
    _add_session(
        sweep_db,
        "s1",
        state="archived",
        archived_reason="user",
        archived_at=NOW - timedelta(days=181),
        last_activity=NOW - timedelta(days=250),
    )
    root = _thread_root(tmp_path, "s1")

    def commit_unarchive():
        assert set_user_archive_state(
            actor_db,
            "s1",
            archived=False,
            now=NOW - timedelta(seconds=1),
        ) is True
        actor_db.commit()

    interleaved = _CommitBeforeFirstCandidateLock(sweep_db, commit_unarchive, monkeypatch)
    report = run_retention_sweep(
        interleaved,
        now=NOW,
        config=CFG,
        workspace=tmp_path,
    )

    assert interleaved.fired is True
    assert report.purged == []
    assert root.exists()
    assert sweep_db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='s1'"
    ).fetchone()[0] == 1
    row = sweep_db.execute(
        "SELECT lifecycle_state, archived_at, archived_reason "
        "FROM threads WHERE id='s1'"
    ).fetchone()
    assert tuple(row) == ("dormant", None, None)


def test_sweep_revalidates_fresh_send_before_purging(tmp_path, monkeypatch):
    path = tmp_path / "send-race.sqlite"
    sweep_db = _db(path)
    actor_db = _peer_db(path)
    _add_session(
        sweep_db,
        "s1",
        state="archived",
        archived_reason="dormancy",
        archived_at=NOW - timedelta(days=181),
        last_activity=NOW - timedelta(days=250),
    )
    root = _thread_root(tmp_path, "s1")

    def commit_send():
        actor_db.execute("BEGIN IMMEDIATE")
        actor_db.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at) "
            "VALUES (?,?,?,?,?)",
            ("fresh", "s1", "user", "resume", _iso(NOW - timedelta(seconds=1))),
        )
        actor_db.commit()

    interleaved = _CommitBeforeFirstCandidateLock(sweep_db, commit_send, monkeypatch)
    report = run_retention_sweep(
        interleaved,
        now=NOW,
        config=CFG,
        workspace=tmp_path,
    )

    assert interleaved.fired is True
    assert report.reactivated == ["s1"]
    assert report.purged == []
    assert root.exists()
    assert sweep_db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='s1'"
    ).fetchone()[0] == 2
    assert sweep_db.execute(
        "SELECT lifecycle_state FROM threads WHERE id='s1'"
    ).fetchone()[0] == "active"


def test_resumed_dormant_thread_reactivates_and_does_not_archive(tmp_path):
    # THE invariant that matters most (transitions are not one-way): a thread
    # that went dormant long ago (dormant_at 31d — past the 30d archive line) but
    # was RESUMED (fresh activity 1d ago) must flip back to active, clear
    # dormant_at, and NOT archive / drop working/ — this sweep or the next.
    db = _db()
    _add_session(db, "s1", state="dormant",
                 dormant_at=NOW - timedelta(days=31),
                 last_activity=NOW - timedelta(days=1))
    root = _thread_root(tmp_path, "s1")
    (root / "working" / "live.txt").write_text("resumed work")

    first = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert first.reactivated == ["s1"]
    assert first.archived == []
    row = db.execute("SELECT lifecycle_state, dormant_at, archived_at FROM threads WHERE id='s1'").fetchone()
    assert row["lifecycle_state"] == "active"
    assert row["dormant_at"] is None
    assert row["archived_at"] is None
    assert (root / "working" / "live.txt").exists()

    # Next sweep at the same instant: stays active, still no archive.
    second = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert second.reactivated == [] and second.archived == []
    assert db.execute("SELECT lifecycle_state FROM threads WHERE id='s1'").fetchone()[0] == "active"
    assert (root / "working" / "live.txt").exists()


def test_explicit_user_archive_not_reactivated_by_activity(tmp_path):
    # The mirror of reactivation: a DELIBERATE user archive (archived flag set)
    # stays archived even with fresh activity — only the user clearing the flag
    # returns it to idle-driven management.
    db = _db()
    _add_session(db, "s1", state="archived", archived_reason="user",
                 last_activity=NOW - timedelta(days=1))
    root = _thread_root(tmp_path, "s1")
    (root / "working" / "x.txt").write_text("tmp")
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert report.reactivated == []
    assert report.archived == []
    assert db.execute("SELECT lifecycle_state FROM threads WHERE id='s1'").fetchone()[0] == "archived"


def test_agent_tool_payloads_trimmed_but_text_preserved(tmp_path):
    # Tool-trim is GLOBAL (policy §1): agent/roster threads — the longest-lived —
    # get their disposable tool payloads trimmed at 30d too, while user/assistant
    # text is always preserved.
    db = _db()
    _add_session(db, "agent", agent_enabled=1, last_activity=NOW - timedelta(days=1))
    old = _iso(NOW - timedelta(days=60))
    db.executemany(
        "INSERT INTO messages (id, thread_id, role, content, created_at) VALUES (?,?,?,?,?)",
        [
            ("t1", "agent", "tool", "K" * 5000, old),
            ("u1", "agent", "user", "keep me", old),
            ("a1", "agent", "assistant", "keep me too", old),
        ],
    )
    db.commit()
    _seed_covering_checkpoint(db, "agent")
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert report.tool_trimmed == 1
    tool = db.execute("SELECT content FROM messages WHERE id='t1'").fetchone()[0]
    assert tool.startswith("[tool output trimmed") and len(tool) < 200
    assert db.execute("SELECT content FROM messages WHERE id='u1'").fetchone()[0] == "keep me"
    assert db.execute("SELECT content FROM messages WHERE id='a1'").fetchone()[0] == "keep me too"
    # And its lifecycle state is untouched — agents are excluded from the machine.
    assert db.execute("SELECT lifecycle_state FROM threads WHERE id='agent'").fetchone()[0] == "active"



# --- tool-I/O trim ----------------------------------------------------------

def test_tool_payloads_trimmed_after_window_keeping_user_assistant(tmp_path):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    old = _iso(NOW - timedelta(days=31))
    db.executemany(
        "INSERT INTO messages (id, thread_id, role, content, created_at) VALUES (?,?,?,?,?)",
        [
            ("t1", "s1", "tool", "X" * 5000, old),
            ("u1", "s1", "user", "keep me", old),
            ("a1", "s1", "assistant", "keep me too", old),
        ],
    )
    db.commit()
    _seed_covering_checkpoint(db, "s1")

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.tool_trimmed == 1
    tool = db.execute("SELECT content FROM messages WHERE id='t1'").fetchone()[0]
    assert tool.startswith("[tool output trimmed") and len(tool) < 200
    assert db.execute("SELECT content FROM messages WHERE id='u1'").fetchone()[0] == "keep me"
    assert db.execute("SELECT content FROM messages WHERE id='a1'").fetchone()[0] == "keep me too"


def test_old_tool_payload_without_checkpoint_is_deferred(tmp_path):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    original = "N" * 5000
    db.execute(
        "INSERT INTO messages (id, thread_id, role, content, created_at, seq) "
        "VALUES (?,?,?,?,?,?)",
        ("t1", "s1", "tool", original, _iso(NOW - timedelta(days=31)), 2),
    )
    db.commit()

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.tool_trimmed == 0
    assert db.execute("SELECT content FROM messages WHERE id='t1'").fetchone()[0] == original


def test_old_tool_payload_with_stale_epoch_checkpoint_is_deferred(tmp_path):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    original = "S" * 5000
    db.execute(
        "INSERT INTO messages (id, thread_id, role, content, created_at, seq) "
        "VALUES (?,?,?,?,?,?)",
        ("t1", "s1", "tool", original, _iso(NOW - timedelta(days=31)), 2),
    )
    db.execute("UPDATE threads SET history_epoch = 1 WHERE id='s1'")
    db.commit()
    _seed_covering_checkpoint(db, "s1", history_epoch=0)

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.tool_trimmed == 0
    assert db.execute("SELECT content FROM messages WHERE id='t1'").fetchone()[0] == original


def test_old_tool_payload_beyond_checkpoint_boundary_is_deferred(tmp_path):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    old = _iso(NOW - timedelta(days=31))
    original = "B" * 5000
    db.executemany(
        "INSERT INTO messages (id, thread_id, role, content, created_at, seq) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("boundary", "s1", "assistant", "covered", old, 2),
            ("t1", "s1", "tool", original, old, 3),
        ],
    )
    db.commit()
    _seed_covering_checkpoint(db, "s1", through_seq=2)

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.tool_trimmed == 0
    assert db.execute("SELECT content FROM messages WHERE id='t1'").fetchone()[0] == original


def test_older_complete_checkpoint_covers_trim_while_refresh_is_pending(tmp_path):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    old = _iso(NOW - timedelta(days=31))
    db.executemany(
        "INSERT INTO messages (id, thread_id, role, content, created_at, seq) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("t1", "s1", "tool", "P" * 5000, old, 2),
            ("requested", "s1", "assistant", "new tail", old, 3),
        ],
    )
    db.commit()
    _seed_covering_checkpoint(db, "s1", through_seq=2)
    db.execute(
        "INSERT INTO checkpoints "
        "(id,thread_id,history_epoch,through_seq,through_message_id,trigger,mode,"
        "status,source_harness,source_model,writer_harness,writer_model,"
        "continuity_base_revision,continuity_base_snapshot_json,started_at) "
        "VALUES ('pending','s1',0,3,'requested','pressure','warm','running',"
        "'native','test-model','native','test-model',0,'[]','t')"
    )
    db.commit()

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.tool_trimmed == 1
    assert db.execute("SELECT content FROM messages WHERE id='t1'").fetchone()[0].startswith(
        "[tool output trimmed"
    )


def test_tool_payload_trim_is_atomic_across_all_covered_rows(tmp_path):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    old = _iso(NOW - timedelta(days=31))
    db.executemany(
        "INSERT INTO messages (id, thread_id, role, content, created_at, seq) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("t1", "s1", "tool", "A" * 5000, old, 2),
            ("t2", "s1", "tool", "B" * 5000, old, 3),
        ],
    )
    db.commit()
    _seed_covering_checkpoint(db, "s1")
    db.executescript(
        """
        CREATE TRIGGER abort_second_tool_trim
        BEFORE UPDATE OF content ON messages
        WHEN new.id = 't2'
        BEGIN
            SELECT RAISE(ABORT, 'injected trim failure');
        END;
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected trim failure"):
        run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    contents = db.execute(
        "SELECT id, content FROM messages WHERE id IN ('t1', 't2') ORDER BY id"
    ).fetchall()
    assert [(row["id"], row["content"]) for row in contents] == [
        ("t1", "A" * 5000),
        ("t2", "B" * 5000),
    ]


def test_tool_payload_trim_skips_coverage_revoked_after_unlocked_select(tmp_path, monkeypatch):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    original = "A" * 5000
    db.execute(
        "INSERT INTO messages (id, thread_id, role, content, created_at, seq) "
        "VALUES (?,?,?,?,?,?)",
        ("t1", "s1", "tool", original, _iso(NOW - timedelta(days=31)), 2),
    )
    db.commit()
    _seed_covering_checkpoint(db, "s1")

    def revoke_coverage():
        db.execute("UPDATE threads SET history_epoch = history_epoch + 1 WHERE id='s1'")
        db.commit()

    report = lifecycle.RetentionReport()
    interleaved = _CommitBeforeFirstCandidateLock(db, revoke_coverage, monkeypatch)
    lifecycle_trims._trim_tool_payloads(interleaved, NOW, CFG, report)

    assert interleaved.fired is True
    assert report.tool_trimmed == 0
    assert db.execute("SELECT content FROM messages WHERE id='t1'").fetchone()[0] == original


def test_tool_payload_trim_reads_before_and_writes_one_bounded_batch_at_a_time(tmp_path, monkeypatch):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    old = _iso(NOW - timedelta(days=31))
    db.executemany(
        "INSERT INTO messages (id, thread_id, role, content, created_at) VALUES (?,?,?,?,?)",
        [(f"t{index}", "s1", "tool", "A" * 5000, old) for index in range(5)],
    )
    db.commit()
    _seed_covering_checkpoint(db, "s1")
    tracked = _TrackToolTrimBatches(db)
    selected_before_writer: list[bool] = []
    original_candidates = lifecycle_trims._trim_candidates

    def track_candidates(connection, cutoff):
        selected_before_writer.append(not tracked.batch_update_counts)
        return original_candidates(connection, cutoff)

    monkeypatch.setattr(lifecycle_trims, "TOOL_TRIM_BATCH_SIZE", 2)
    monkeypatch.setattr(lifecycle_trims, "_trim_candidates", track_candidates)
    report = lifecycle.RetentionReport()

    lifecycle_trims._trim_tool_payloads(tracked, NOW, CFG, report)

    assert selected_before_writer == [True]
    assert tracked.batch_update_counts == [2, 2, 1]
    assert report.tool_trimmed == 5


def _seed_trimmable_tool_rows(db, sizes: list[int]) -> None:
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    old = _iso(NOW - timedelta(days=31))
    db.executemany(
        "INSERT INTO messages (id, thread_id, role, content, created_at) VALUES (?,?,?,?,?)",
        [(f"t{index}", "s1", "tool", "A" * size, old) for index, size in enumerate(sizes)],
    )
    db.commit()
    _seed_covering_checkpoint(db, "s1")


def test_tool_payload_trim_batch_stops_on_content_budget(monkeypatch):
    db = _db()
    _seed_trimmable_tool_rows(db, [6000, 6000, 3000, 12000, 1000])
    tracked = _TrackToolTrimBatches(db)
    monkeypatch.setattr(lifecycle_trims, "TOOL_TRIM_BATCH_CHARS", 10000)
    report = lifecycle.RetentionReport()

    lifecycle_trims._trim_tool_payloads(tracked, NOW, CFG, report)

    # 6000+6000 spends the budget; 3000+12000 does too; the last row goes alone.
    assert tracked.batch_update_counts == [2, 2, 1]
    assert report.tool_trimmed == 5


def test_tool_payload_trim_batch_stops_on_time_budget(monkeypatch):
    db = _db()
    _seed_trimmable_tool_rows(db, [100] * 5)
    tracked = _TrackToolTrimBatches(db)
    ticks = iter(index * 0.1 for index in range(1000))
    monkeypatch.setattr(lifecycle_trims, "_clock", lambda: next(ticks))
    monkeypatch.setattr(lifecycle_trims, "TOOL_TRIM_BATCH_SECONDS", 0.25)
    report = lifecycle.RetentionReport()

    lifecycle_trims._trim_tool_payloads(tracked, NOW, CFG, report)

    # Each batch reads the clock at start (t), then before rows 2 (t+0.1),
    # 3 (t+0.2) and 4 (t+0.3, spent): three rows, then the next batch.
    assert tracked.batch_update_counts == [3, 2]
    assert report.tool_trimmed == 5


def test_tool_payload_trim_row_over_budget_still_progresses(monkeypatch):
    db = _db()
    _seed_trimmable_tool_rows(db, [5000] * 3)
    tracked = _TrackToolTrimBatches(db)
    monkeypatch.setattr(lifecycle_trims, "TOOL_TRIM_BATCH_CHARS", 1)
    report = lifecycle.RetentionReport()

    lifecycle_trims._trim_tool_payloads(tracked, NOW, CFG, report)

    assert tracked.batch_update_counts == [1, 1, 1]
    assert report.tool_trimmed == 3


def test_tool_payload_coverage_check_seeks_checkpoints_before_boundary():
    # Walking ``boundary`` by a seq range made each row cost O(later messages x
    # checkpoints) and held the writer for 36.6 s in production (#2948).
    db = _db()
    plan = [
        row[3]
        for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM messages AS m WHERE m.id = ? AND "
            f"{lifecycle_trims._TRIMMABLE_TOOL_PAYLOAD}",
            ("t1", _iso(NOW), 1, "x"),
        )
    ]
    checkpoint = next(i for i, step in enumerate(plan) if step.startswith("SEARCH p "))
    boundary = next(i for i, step in enumerate(plan) if step.startswith("SEARCH boundary "))
    assert checkpoint < boundary
    assert "seq=?" in plan[boundary]


def test_recent_tool_payloads_not_trimmed(tmp_path):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    db.execute(
        "INSERT INTO messages (id, thread_id, role, content, created_at) VALUES (?,?,?,?,?)",
        ("t1", "s1", "tool", "Y" * 5000, _iso(NOW - timedelta(days=10))),
    )
    db.commit()
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert report.tool_trimmed == 0
    assert len(db.execute("SELECT content FROM messages WHERE id='t1'").fetchone()[0]) == 5000


# --- working/ tmp trim ------------------------------------------------------

@pytest.fixture
def mtime_as_added_time(monkeypatch):
    """Let a test set an entry's added time with os.utime.

    ctime and birthtime cannot be backdated, so tests that age entries on the
    fixed NOW clock read the lstat mtime as the added time.
    """
    monkeypatch.setattr(lifecycle_trims, "_entry_times", lambda st: st.st_mtime)


def _age(path: Path, days: int) -> None:
    import os

    t = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (t, t), follow_symlinks=False)


def _age_tree(root: Path, days: int) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        _age(path, days)
    _age(root, days)


def test_working_tmp_trimmed_after_ttl(tmp_path, mtime_as_added_time):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    root = _thread_root(tmp_path, "s1")
    stale = root / "working" / "stale.txt"
    fresh = root / "working" / "fresh.txt"
    stale.write_text("old")
    fresh.write_text("new")
    import os
    old_t = (NOW - timedelta(days=8)).timestamp()
    os.utime(stale, (old_t, old_t))

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 1
    assert not stale.exists()
    assert fresh.exists()


def test_working_tmp_trim_retires_deleted_and_already_missing_artifacts(
    tmp_path, mtime_as_added_time,
):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    root = _thread_root(tmp_path, "s1")
    stale = root / "working" / "stale.txt"
    fresh = root / "working" / "fresh.txt"
    missing = root / "working" / "already-missing.txt"
    stale.write_text("old")
    fresh.write_text("new")
    import os

    old_t = (NOW - timedelta(days=8)).timestamp()
    os.utime(stale, (old_t, old_t))
    for artifact_id, path in (
        ("stale", stale),
        ("fresh", fresh),
        ("missing", missing),
    ):
        _add_artifact_identity(db, artifact_id, path=path, retention="ephemeral")
        _add_artifact_registration(db, artifact_id, "s1")
    db.commit()

    first = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert first.working_files_trimmed == 1
    assert not stale.exists()
    assert fresh.exists()
    assert {
        row["id"]: row["state"]
        for row in db.execute(
            "SELECT id,state FROM artifact_identities ORDER BY id"
        ).fetchall()
    } == {
        "fresh": "active",
        "missing": "tombstone",
        "stale": "tombstone",
    }
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_registrations"
    ).fetchone()[0] == 3

    second = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert second.working_files_trimmed == 0
    assert {
        row["id"]: row["state"]
        for row in db.execute(
            "SELECT id,state FROM artifact_identities ORDER BY id"
        ).fetchall()
    } == {
        "fresh": "active",
        "missing": "tombstone",
        "stale": "tombstone",
    }


def test_working_tmp_reconciliation_uses_one_fixed_query_for_all_live_threads(
    tmp_path,
):
    db = _db()
    for sid in ("s1", "s2"):
        _add_session(db, sid, last_activity=NOW - timedelta(days=1))
        missing = _thread_root(tmp_path, sid) / "working" / "missing.txt"
        artifact_id = f"{sid}-missing"
        _add_artifact_identity(db, artifact_id, path=missing, retention="ephemeral")
        _add_artifact_registration(db, artifact_id, sid)
    db.commit()

    statements: list[str] = []
    original_variable_limit = db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 3)
    db.set_trace_callback(statements.append)
    try:
        lifecycle_trims._trim_working_tmp(
            db,
            NOW,
            CFG,
            tmp_path,
            lifecycle.RetentionReport(),
        )
    finally:
        db.set_trace_callback(None)
        db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, original_variable_limit)

    registry_queries = [
        statement
        for statement in statements
        if "FROM artifact_identities i" in statement
        and "JOIN artifact_registrations r" in statement
        and "i.path IS NOT NULL" in statement
    ]
    begin_immediates = [
        statement
        for statement in statements
        if statement.strip().upper() == "BEGIN IMMEDIATE"
    ]
    assert len(registry_queries) == 1
    assert len(begin_immediates) == 1
    assert {
        row["id"]: row["state"]
        for row in db.execute(
            "SELECT id,state FROM artifact_identities ORDER BY id"
        ).fetchall()
    } == {
        "s1-missing": "tombstone",
        "s2-missing": "tombstone",
    }


def test_single_root_tmp_reconciliation_keeps_owner_predicate(tmp_path):
    from registry import service as registry_service

    db = _db()
    for sid in ("s1", "s2"):
        missing = _thread_root(tmp_path, sid) / "working" / "missing.txt"
        artifact_id = f"{sid}-missing"
        _add_artifact_identity(db, artifact_id, path=missing, retention="ephemeral")
        _add_artifact_registration(db, artifact_id, sid)
    db.commit()

    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        registry_service.retire_missing_temporary_under(
            tmp_path / ".threads" / "s1" / "working",
            parent_thread_id="s1",
            db=db,
        )
    finally:
        db.set_trace_callback(None)

    registry_queries = [
        statement
        for statement in statements
        if "FROM artifact_identities i" in statement
        and "JOIN artifact_registrations r" in statement
        and "i.path IS NOT NULL" in statement
    ]
    assert len(registry_queries) == 1
    assert "r.parent_thread_id='s1'" in "".join(registry_queries[0].split())
    assert {
        row["id"]: row["state"]
        for row in db.execute(
            "SELECT id,state FROM artifact_identities ORDER BY id"
        ).fetchall()
    } == {
        "s1-missing": "tombstone",
        "s2-missing": "active",
    }


def test_working_tmp_trim_keeps_freshly_added_files_with_old_mtimes(tmp_path):
    """#2641: wheel-installed files and venv links keep upstream mtimes.

    Real clock, real ctime: an entry added just now is young whatever its mtime.
    """
    import os
    from datetime import datetime

    now = datetime.now(timezone.utc)
    db = _db()
    _add_session(db, "s1", last_activity=now - timedelta(days=1))
    working = _thread_root(tmp_path, "s1") / "working"
    interpreter = tmp_path / "uv-python" / "python3.11"
    interpreter.parent.mkdir()
    interpreter.write_text("#!")
    venv_bin = working / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(interpreter)
    wheel_file = working / ".venv" / "jinja2.py"
    wheel_file.write_text("upstream")
    months_ago = (now - timedelta(days=90)).timestamp()
    os.utime(interpreter, (months_ago, months_ago))
    os.utime(wheel_file, (months_ago, months_ago))

    report = run_retention_sweep(db, now=now, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 0
    assert (venv_bin / "python").is_symlink()
    assert wheel_file.exists()
    assert interpreter.exists()


def test_working_tmp_trim_never_follows_links(tmp_path, mtime_as_added_time):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    working = _thread_root(tmp_path, "s1") / "working"
    old_target = tmp_path / "elsewhere" / "old.txt"
    old_target.parent.mkdir()
    old_target.write_text("keep")
    _age(old_target, 90)
    young_link = working / "young-link"
    young_link.symlink_to(old_target)
    old_dir_target = tmp_path / "elsewhere" / "tree"
    (old_dir_target / "inner.txt").parent.mkdir()
    (old_dir_target / "inner.txt").write_text("keep")
    _age_tree(old_dir_target, 90)
    old_link = working / "old-dir-link"
    old_link.symlink_to(old_dir_target, target_is_directory=True)
    _age(old_link, 90)

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 1
    assert young_link.is_symlink()
    assert not old_link.exists() and not old_link.is_symlink()
    assert old_target.read_text() == "keep"
    assert (old_dir_target / "inner.txt").read_text() == "keep"


def test_working_tmp_trim_removes_a_directory_whole_or_not_at_all(
    tmp_path, mtime_as_added_time,
):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    working = _thread_root(tmp_path, "s1") / "working"
    mixed = working / "mixed"
    (mixed / "deep").mkdir(parents=True)
    (mixed / "deep" / "old.txt").write_text("old")
    (mixed / "new.txt").write_text("new")
    _age_tree(mixed, 30)
    _age(mixed / "new.txt", 1)
    stale = working / "stale"
    (stale / "deep").mkdir(parents=True)
    (stale / "deep" / "a.txt").write_text("a")
    _age_tree(stale, 30)

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 1
    assert not stale.exists()
    assert (mixed / "deep" / "old.txt").exists()
    assert (mixed / "new.txt").exists()


def test_working_tmp_trim_reports_a_failed_removal_and_finishes_it_later(
    tmp_path, mtime_as_added_time, monkeypatch,
):
    real_remove_entry = lifecycle_trims._remove_entry
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    working = _thread_root(tmp_path, "s1") / "working"
    stale = working / "stale"
    (stale / "a.txt").parent.mkdir(parents=True)
    (stale / "a.txt").write_text("a")
    _age_tree(stale, 30)

    def interrupted(*_args):
        raise OSError("interrupted")

    monkeypatch.setattr(lifecycle_trims, "_remove_entry", interrupted)
    first = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert (first.working_files_trimmed, first.working_trim_errors) == (0, 1)
    assert first.as_dict()["working_trim_errors"] == 1
    assert (stale / "a.txt").exists()

    monkeypatch.setattr(lifecycle_trims, "_remove_entry", real_remove_entry)
    second = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert (second.working_files_trimmed, second.working_trim_errors) == (1, 0)
    assert list(working.iterdir()) == []


def test_working_tmp_trim_keeps_an_entry_added_after_the_unit_was_judged(
    tmp_path, mtime_as_added_time, monkeypatch,
):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    working = _thread_root(tmp_path, "s1") / "working"
    unit = working / "unit"
    (unit / "deep").mkdir(parents=True)
    (unit / "deep" / "old.txt").write_text("old")
    (unit / "other.txt").write_text("old")
    _age_tree(unit, 30)
    real_is_stale = lifecycle_trims._unit_is_stale

    def judged_then_written(path, cutoff):
        stale = real_is_stale(path, cutoff)
        (unit / "deep" / "new.txt").write_text("written mid-sweep")
        _age(unit / "deep" / "new.txt", 0)
        return stale

    monkeypatch.setattr(lifecycle_trims, "_unit_is_stale", judged_then_written)
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert (report.working_files_trimmed, report.working_trim_errors) == (0, 0)
    assert (unit / "deep" / "new.txt").read_text() == "written mid-sweep"
    assert not (unit / "deep" / "old.txt").exists()
    assert not (unit / "other.txt").exists()


def test_working_tmp_trim_reports_a_failed_keep_marker_check(
    tmp_path, mtime_as_added_time, monkeypatch,
):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    working = _thread_root(tmp_path, "s1") / "working"
    stale = working / "stale.txt"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("a")
    _age(stale, 30)
    real_lstat_or_none = lifecycle_trims._lstat_or_none

    def unreadable_marker(path):
        if path.name == lifecycle_trims.WORKING_KEEP_MARKER:
            raise PermissionError("denied")
        return real_lstat_or_none(path)

    monkeypatch.setattr(lifecycle_trims, "_lstat_or_none", unreadable_marker)
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert (report.working_files_trimmed, report.working_trim_errors) == (0, 1)
    assert stale.exists()


def test_working_tmp_trim_reports_an_unreadable_unit_and_keeps_it(
    tmp_path, mtime_as_added_time,
):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    working = _thread_root(tmp_path, "s1") / "working"
    locked = working / "locked"
    (locked / "inner").mkdir(parents=True)
    (locked / "inner" / "a.txt").write_text("a")
    _age_tree(locked, 30)
    (locked / "inner").chmod(0)
    try:
        report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    finally:
        (locked / "inner").chmod(0o755)

    assert (report.working_files_trimmed, report.working_trim_errors) == (0, 1)
    assert (locked / "inner" / "a.txt").exists()


def test_working_tmp_trim_skips_a_thread_with_a_running_turn(
    tmp_path, mtime_as_added_time,
):
    db = _db()
    _add_session(db, "busy", last_activity=NOW - timedelta(days=1))
    _add_session(db, "idle", last_activity=NOW - timedelta(days=1))
    db.execute(
        "INSERT INTO active_turns (id, thread_id, status, started_at) "
        "VALUES ('t1', 'busy', 'running', ?)",
        (NOW.isoformat(),),
    )
    db.commit()
    units = {}
    for sid in ("busy", "idle"):
        unit = _thread_root(tmp_path, sid) / "working" / "stale"
        (unit / "a.txt").parent.mkdir(parents=True)
        (unit / "a.txt").write_text("a")
        _age_tree(unit, 30)
        units[sid] = unit

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 1
    assert (units["busy"] / "a.txt").exists()
    assert not units["idle"].exists()


def test_working_tmp_trim_removes_real_entries_past_the_ttl(tmp_path):
    """Production added-time rule, real ctime: a sweep a TTL later removes it."""
    from datetime import datetime

    later = datetime.now(timezone.utc) + timedelta(days=CFG.working_ttl_days + 1)
    db = _db()
    _add_session(db, "s1", last_activity=later - timedelta(days=1))
    working = _thread_root(tmp_path, "s1") / "working"
    (working / "tree" / "deep").mkdir(parents=True)
    (working / "tree" / "deep" / "a.txt").write_text("a")
    (working / "note.txt").write_text("n")

    report = run_retention_sweep(db, now=later, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 2
    assert list(working.iterdir()) == []


@pytest.mark.parametrize("marker_at", ["unit", "nested", "working"])
def test_working_tmp_keep_marker_keeps_the_whole_unit(
    tmp_path, mtime_as_added_time, marker_at,
):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    working = _thread_root(tmp_path, "s1") / "working"
    kept = working / "env"
    (kept / "lib").mkdir(parents=True)
    (kept / "lib" / "mod.py").write_text("x")
    marker_dir = {"unit": kept, "nested": kept / "lib", "working": working}[marker_at]
    (marker_dir / ".corsia-keep").write_text("")
    _age_tree(kept, 30)
    if marker_at == "working":
        _age(working / ".corsia-keep", 30)

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 0
    assert (kept / "lib" / "mod.py").exists()


def test_working_tmp_trim_never_touches_other_thread_folders(
    tmp_path, mtime_as_added_time,
):
    db = _db()
    _add_session(db, "s1", last_activity=NOW - timedelta(days=1))
    root = _thread_root(tmp_path, "s1")
    durable = root / "scratch" / "env" / "old.txt"
    durable.parent.mkdir(parents=True)
    durable.write_text("durable")
    _age_tree(root / "scratch", 90)

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 0
    assert durable.read_text() == "durable"


def test_settled_worker_working_unit_uses_normal_ttl(tmp_path, mtime_as_added_time):
    db = _db()
    _add_session(
        db, "settled", state="archived", archived_reason=lifecycle.SETTLED_WORKER_REASON,
        archived_at=NOW - timedelta(days=8), spawned_by="parent",
    )
    root = _thread_root(tmp_path, "settled")
    stale = root / "working" / "snapshot"
    stale.write_text("old")
    durable = root / "scratch" / "snapshot"
    durable.parent.mkdir(parents=True)
    durable.write_text("keep")
    _age(stale, 8)
    _age(durable, 30)

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 1
    assert not stale.exists()
    assert durable.read_text() == "keep"


def test_settled_worker_working_unit_under_normal_ttl_is_kept(tmp_path, mtime_as_added_time):
    db = _db()
    _add_session(
        db, "settled", state="archived", archived_reason=lifecycle.SETTLED_WORKER_REASON,
        archived_at=NOW - timedelta(days=6), spawned_by="parent",
    )
    fresh = _thread_root(tmp_path, "settled") / "working" / "fresh"
    fresh.write_text("new")
    _age(fresh, 6)

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 0
    assert fresh.exists()


@pytest.mark.parametrize("reason", ["user", "dormancy"])
def test_non_settled_archived_worker_working_is_untouched(tmp_path, mtime_as_added_time, reason):
    db = _db()
    _add_session(
        db, "archived", state="archived", archived_reason=reason,
        archived_at=NOW - timedelta(days=2), spawned_by="parent",
    )
    stale = _thread_root(tmp_path, "archived") / "working" / "stale"
    stale.write_text("old")
    _age(stale, 30)

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 0
    assert stale.exists()


@pytest.mark.parametrize("spawned_by,agent_enabled", [(None, 0), ("parent", 1)])
def test_non_temporary_or_agent_settled_archived_working_is_untouched(
    tmp_path, mtime_as_added_time, spawned_by, agent_enabled,
):
    db = _db()
    _add_session(
        db, "excluded", state="archived", archived_reason=lifecycle.SETTLED_WORKER_REASON,
        archived_at=NOW - timedelta(days=2), spawned_by=spawned_by,
        agent_enabled=agent_enabled,
    )
    stale = _thread_root(tmp_path, "excluded") / "working" / "stale"
    stale.write_text("old")
    _age(stale, 30)

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 0
    assert stale.exists()


def test_settled_worker_keep_marker_prevents_working_trim(tmp_path, mtime_as_added_time):
    db = _db()
    _add_session(
        db, "settled", state="archived", archived_reason=lifecycle.SETTLED_WORKER_REASON,
        archived_at=NOW - timedelta(days=2), spawned_by="parent",
    )
    working = _thread_root(tmp_path, "settled") / "working"
    kept = working / "snapshot"
    kept.write_text("old")
    (working / ".corsia-keep").write_text("")
    _age(kept, 30)

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 0
    assert kept.exists()


def test_settled_worker_with_running_turn_is_skipped(tmp_path, mtime_as_added_time):
    db = _db()
    _add_session(
        db, "settled", state="archived", archived_reason=lifecycle.SETTLED_WORKER_REASON,
        archived_at=NOW - timedelta(days=2), spawned_by="parent",
    )
    db.execute(
        "INSERT INTO active_turns (id, thread_id, status, started_at) VALUES (?,?,?,?)",
        ("turn", "settled", "running", NOW.isoformat()),
    )
    db.commit()
    stale = _thread_root(tmp_path, "settled") / "working" / "stale"
    stale.write_text("old")
    _age(stale, 30)

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.working_files_trimmed == 0
    assert stale.exists()


def test_working_tmp_trim_includes_agent_threads(tmp_path, mtime_as_added_time):
    # Item 2 consistency: working/ trim is global — agent/roster threads included
    # (matches the tool-I/O trim; disposable tmp accumulates most on long-lived
    # threads).
    db = _db()
    _add_session(db, "agent", agent_enabled=1, last_activity=NOW - timedelta(days=1))
    root = _thread_root(tmp_path, "agent")
    stale = root / "working" / "stale.txt"
    stale.write_text("old")
    import os
    old_t = (NOW - timedelta(days=8)).timestamp()
    os.utime(stale, (old_t, old_t))
    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert report.working_files_trimmed == 1
    assert not stale.exists()


def test_purge_archives_pathless_owned_tmp_only(tmp_path):
    db = _db()
    _add_session(
        db,
        "s1",
        state="archived",
        archived_reason="dormancy",
        archived_at=NOW - timedelta(days=181),
        last_activity=NOW - timedelta(days=250),
    )
    _thread_root(tmp_path, "s1")
    _add_artifact_identity(db, "pathless-tmp", retention="ephemeral")
    _add_artifact_registration(db, "pathless-tmp", "s1")
    _add_artifact_identity(db, "pathless-durable", retention="durable")
    _add_artifact_registration(db, "pathless-durable", "s1")
    db.commit()

    report = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert report.purged == ["s1"]
    assert {
        row["id"]: row["state"]
        for row in db.execute(
            "SELECT id,state FROM artifact_identities ORDER BY id"
        ).fetchall()
    } == {
        "pathless-durable": "active",
        "pathless-tmp": "archived",
    }


def test_full_sweep_is_idempotent(tmp_path):
    db = _db()
    # one of each transition candidate
    _add_session(db, "d", last_activity=NOW - timedelta(days=20))
    _add_session(db, "a", state="dormant", dormant_at=NOW - timedelta(days=31),
                 last_activity=NOW - timedelta(days=45))
    _add_session(db, "p", state="archived", archived_reason="dormancy", archived_at=NOW - timedelta(days=181),
                 last_activity=NOW - timedelta(days=250))
    for sid in ("d", "a", "p"):
        _thread_root(tmp_path, sid)
    db.execute(
        "INSERT INTO messages (id, thread_id, role, content, created_at) VALUES (?,?,?,?,?)",
        ("t-old", "d", "tool", "Z" * 5000, _iso(NOW - timedelta(days=40))),
    )
    db.commit()
    _seed_covering_checkpoint(db, "d")

    first = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert first.dormant == ["d"] and first.archived == ["a"] and first.purged == ["p"]
    assert first.tool_trimmed == 1

    # Re-run at the SAME instant: nothing new transitions or trims.
    second = run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)
    assert second.dormant == []
    assert second.archived == []
    assert second.purged == []
    assert second.tool_trimmed == 0
    assert second.working_files_trimmed == 0


def test_purge_destroys_continuity_with_the_transcript(tmp_path):
    """Continuity is condensed conversation; it must not outlive the thread.

    Purge keeps the sessions row, so the foreign-key cascade never fires here.
    Only the explicit delete stops a purged thread's constraints surviving.
    """
    db = _db()
    _add_session(
        db,
        "s1",
        state="archived",
        archived_reason="dormancy",
        archived_at=NOW - timedelta(days=181),
        last_activity=NOW - timedelta(days=250),
    )
    db.execute(
        "INSERT INTO continuity_store "
        "(thread_id, history_epoch, revision, next_entry_id, updated_at) "
        "VALUES ('s1', 0, 1, 2, 't')"
    )
    db.execute(
        "INSERT INTO continuity_entries "
        "(thread_id, history_epoch, entry_id, field, body, revision, state, "
        "created_at, updated_at) VALUES "
        "('s1', 0, 1, 'active_constraints', '{}', 1, 'active', 't', 't')"
    )
    db.execute(
        """INSERT INTO lesson_placement_checks(
            thread_id,history_epoch,entry_id,revision,check_name,flag,created_at
        ) VALUES ('s1',0,1,1,'incident-details','date','t')"""
    )
    db.commit()

    run_retention_sweep(db, now=NOW, config=CFG, workspace=tmp_path)

    assert db.execute(
        "SELECT COUNT(*) FROM continuity_entries WHERE thread_id='s1'"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM continuity_store WHERE thread_id='s1'"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM lesson_placement_checks WHERE thread_id='s1'"
    ).fetchone()[0] == 0
