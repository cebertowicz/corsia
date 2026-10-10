
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

from cc_runtime_fixtures import (
    StubCCInvocation,
    cc_target,
    feed_main_with_persisted_input,
)
from infra.schema import SCHEMA_SQL

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


_SCHEMA = SCHEMA_SQL


@pytest.fixture(autouse=True)
def _clean_live():
    import subagents.cc_runtime as rt
    rt._LIVE.reset()
    yield
    rt._LIVE.reset()


@pytest.fixture
def _cold_start_db(tmp_path, monkeypatch):
    """Point the shared DB owner at one real cold-start SQLite file."""
    from infra import config, database
    import subagents.cc_runtime_helpers as cc_runtime_helpers
    from tests.conftest import _reset_db_pool

    db_path = tmp_path / "cold_start.db"

    def _make_conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    setup = _make_conn()
    setup.executescript(_SCHEMA)
    setup.commit()
    setup.close()

    _reset_db_pool(database)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(
        cc_runtime_helpers, "_validate_working_dir", lambda *a, **k: None
    )

    yield _make_conn
    _reset_db_pool(database)


class _FakeCC(StubCCInvocation):
    """A CC subprocess stub that starts cleanly and is alive (returncode None,
    so the accept path's liveness reconcile leaves it in place)."""

    returncode = None

    def __init__(self, *a, **k):
        self.session_id = k.get("session_id", "fake-cc-sid")
        self.resume_session_id = k.get("resume_session_id")
        self.working_dir = k.get("working_dir")
        self.reconstructed_transcript_path = k.get("reconstructed_transcript_path")

    async def start(self):
        pass

    async def feed(self, text, *, on_write_observed, on_accepted, input_id=None):
        on_write_observed()
        on_accepted()

    async def terminate(self):
        pass


class TestColdStartCommit:
    """``feed_main`` cold-start (start_cc_for_row) must durably commit the seed
    row + cc_session registration on the ``db=None`` prod path — the seed is
    committed at start, before the first feed."""

    @pytest.mark.asyncio
    async def test_seed_row_committed_on_db_none_path(
        self, _cold_start_db, tmp_path, monkeypatch
    ):
        import subagents.cc_runtime as rt
        import subagents.cc_destination as cc_destination
        import subagents.cc_feed_transition as cc_feed_transition
        import subagents.cc_startup_process as cc_startup_process

        make_conn = _cold_start_db
        conn = make_conn()
        conn.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
            "VALUES ('sm-commit', 'u1', 'claude-code/opus', 't', 't')"
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(cc_startup_process, "CCStreamSession", _FakeCC)
        monkeypatch.setattr(
            cc_destination, "_main_working_dir", lambda pid, db: str(tmp_path)
        )
        # The stale-cwd respawn gate resolves the session's default via the same
        # _main_working_dir; keep this fixture's working-dir override consistent
        # across both seams (as production is — one function) so a freshly
        # cold-started session is not misread as stale and respawned (#841).
        monkeypatch.setattr(
            cc_feed_transition, "_main_working_dir", lambda pid, db: str(tmp_path)
        )

        try:
            await feed_main_with_persisted_input(
                "sm-commit", "task text", target=cc_target("opus"),
                binary="/fake/binary",
                claude_dir=str(tmp_path / "no-claude-home"),
                broadcast=lambda s, m: None,
            )
        finally:
            rt._LIVE.pop("sm-commit", None)

        reader = make_conn()
        roles = [r["role"] for r in reader.execute(
            "SELECT role FROM messages WHERE thread_id = 'sm-commit'"
        ).fetchall()]
        reader.close()

        assert "system" in roles, (
            "feed_main cold-start must commit the seed row before closing _db on "
            "the db=None prod path — seed durability regression guard."
        )

    @pytest.mark.asyncio
    async def test_cold_start_does_not_hold_sqlite_writer_during_process_start(
        self, _cold_start_db, tmp_path, monkeypatch
    ):
        import subagents.cc_runtime as rt
        import subagents.cc_destination as cc_destination
        import subagents.cc_feed_transition as cc_feed_transition
        import subagents.cc_startup_process as cc_startup_process

        make_conn = _cold_start_db
        conn = make_conn()
        conn.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
            "VALUES ('sm-lock', 'u1', 'claude-code/opus', 't', 't')"
        )
        conn.commit()
        conn.close()
        writer_available: list[bool] = []

        class _ProbeCC(_FakeCC):
            async def start(self):
                probe = make_conn()
                probe.execute("PRAGMA busy_timeout=1")
                try:
                    probe.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError:
                    writer_available.append(False)
                else:
                    writer_available.append(True)
                    probe.rollback()
                finally:
                    probe.close()

        monkeypatch.setattr(cc_startup_process, "CCStreamSession", _ProbeCC)
        monkeypatch.setattr(
            cc_destination, "_main_working_dir", lambda _pid, _db: str(tmp_path)
        )
        monkeypatch.setattr(
            cc_feed_transition, "_main_working_dir", lambda _pid, _db: str(tmp_path)
        )

        try:
            await feed_main_with_persisted_input(
                "sm-lock", "task text", target=cc_target("opus"),
                binary="/fake/binary",
                claude_dir=str(tmp_path / "no-claude-home"),
                broadcast=lambda _session, _message: None,
            )
        finally:
            rt._LIVE.pop("sm-lock", None)

        assert writer_available == [True]

    @pytest.mark.asyncio
    async def test_cold_start_replaces_prior_provider_identity(
        self, _cold_start_db, tmp_path, monkeypatch
    ):
        import subagents.cc_destination as cc_destination
        import subagents.cc_feed_transition as cc_feed_transition
        import subagents.cc_runtime as rt
        import subagents.cc_startup_process as cc_startup_process
        from registry.listing import find_for_thread
        from registry.service import register_cc_session

        make_conn = _cold_start_db
        conn = make_conn()
        conn.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
            "VALUES ('sm-replace', 'u1', 'claude-code/opus', 't', 't')"
        )
        conn.commit()
        register_cc_session("sm-replace", "prior-provider", db=conn)
        conn.close()
        monkeypatch.setattr(cc_startup_process, "CCStreamSession", _FakeCC)
        monkeypatch.setattr(
            cc_destination, "_main_working_dir", lambda _pid, _db: str(tmp_path)
        )
        monkeypatch.setattr(
            cc_feed_transition, "_main_working_dir", lambda _pid, _db: str(tmp_path)
        )

        try:
            await feed_main_with_persisted_input(
                "sm-replace", "task text", target=cc_target("opus"),
                binary="/fake/binary", claude_dir=str(tmp_path / "cc-home"),
                allow_provider_resume=False,
                broadcast=lambda _session, _message: None,
            )
        finally:
            rt._LIVE.pop("sm-replace", None)

        reader = make_conn()
        current = find_for_thread(
            "sm-replace", "claude-code", "session", db=reader
        )
        prior = reader.execute(
            "SELECT state FROM artifact_identities WHERE external_id=?",
            ("prior-provider",),
        ).fetchone()
        reader.close()
        assert current is not None
        assert current["external_id"] != "prior-provider"
        assert prior["state"] == "archived"

    @pytest.mark.parametrize("has_prior", [False, True])
    @pytest.mark.asyncio
    async def test_failed_cold_start_restores_exact_prior_identity(
        self, _cold_start_db, tmp_path, monkeypatch, has_prior
    ):
        import subagents.cc_destination as cc_destination
        import subagents.cc_feed_transition as cc_feed_transition
        import subagents.cc_runtime as rt
        import subagents.cc_startup_process as cc_startup_process
        from registry.listing import find_for_thread
        from registry.service import register_cc_session

        class _StartFails(_FakeCC):
            async def start(self):
                raise RuntimeError("simulated start failure")

        make_conn = _cold_start_db
        conn = make_conn()
        conn.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
            "VALUES ('sm-rollback', 'u1', 'claude-code/opus', 't', 't')"
        )
        conn.commit()
        if has_prior:
            register_cc_session("sm-rollback", "prior-provider", db=conn)
        conn.close()
        monkeypatch.setattr(cc_startup_process, "CCStreamSession", _StartFails)
        monkeypatch.setattr(
            cc_destination, "_main_working_dir", lambda _pid, _db: str(tmp_path)
        )
        monkeypatch.setattr(
            cc_feed_transition, "_main_working_dir", lambda _pid, _db: str(tmp_path)
        )

        with pytest.raises(RuntimeError, match="simulated start failure"):
            await feed_main_with_persisted_input(
                "sm-rollback", "task text", target=cc_target("opus"),
                binary="/fake/binary", claude_dir=str(tmp_path / "cc-home"),
                allow_provider_resume=False,
                broadcast=lambda _session, _message: None,
            )
        assert rt._LIVE.get("sm-rollback") is None

        reader = make_conn()
        current = find_for_thread(
            "sm-rollback", "claude-code", "session", db=reader
        )
        active_count = reader.execute(
            "SELECT COUNT(*) AS count FROM artifact_identities "
            "WHERE integration='claude-code' AND type='session' "
            "AND state='active'"
        ).fetchone()["count"]
        reader.close()
        assert active_count == int(has_prior)
        assert (current["external_id"] if current else None) == (
            "prior-provider" if has_prior else None
        )

    @pytest.mark.asyncio
    async def test_unproved_process_death_keeps_new_identity_and_guard(
        self, _cold_start_db, tmp_path, monkeypatch
    ):
        import subagents.cc_destination as cc_destination
        import subagents.cc_feed_transition as cc_feed_transition
        import subagents.cc_runtime as rt
        import subagents.cc_startup_process as cc_startup_process
        from registry.listing import find_for_thread
        from registry.service import register_cc_session

        class _UnprovedDeath(_FakeCC):
            async def start(self):
                raise RuntimeError("simulated start failure")

            async def terminate(self):
                raise RuntimeError("simulated unproved process death")

        make_conn = _cold_start_db
        conn = make_conn()
        conn.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
            "VALUES ('sm-guard', 'u1', 'claude-code/opus', 't', 't')"
        )
        conn.commit()
        register_cc_session("sm-guard", "prior-provider", db=conn)
        conn.close()
        monkeypatch.setattr(cc_startup_process, "CCStreamSession", _UnprovedDeath)
        monkeypatch.setattr(
            cc_destination, "_main_working_dir", lambda _pid, _db: str(tmp_path)
        )
        monkeypatch.setattr(
            cc_feed_transition, "_main_working_dir", lambda _pid, _db: str(tmp_path)
        )

        with pytest.raises(RuntimeError, match="unproved process death"):
            await feed_main_with_persisted_input(
                "sm-guard", "task text", target=cc_target("opus"),
                binary="/fake/binary", claude_dir=str(tmp_path / "cc-home"),
                allow_provider_resume=False,
                broadcast=lambda _session, _message: None,
            )

        live = rt._LIVE.get("sm-guard")
        assert live is not None
        reader = make_conn()
        current = find_for_thread(
            "sm-guard", "claude-code", "session", db=reader
        )
        reader.close()
        assert current["external_id"] == live.session.session_id
        assert current["external_id"] != "prior-provider"

    @pytest.mark.asyncio
    async def test_seed_row_persists_on_feed_failure(
        self, _cold_start_db, tmp_path, monkeypatch
    ):
        import subagents.cc_runtime as rt
        import subagents.cc_destination as cc_destination
        import subagents.cc_feed_transition as cc_feed_transition
        import subagents.cc_startup_process as cc_startup_process

        make_conn = _cold_start_db
        conn = make_conn()
        conn.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
            "VALUES ('sm-fail', 'u1', 'claude-code/opus', 't', 't')"
        )
        conn.commit()
        conn.close()

        class _FakeCCFails(_FakeCC):
            async def feed(self, text, *, on_write_observed, on_accepted, input_id=None):
                raise RuntimeError("simulated feed failure")

        monkeypatch.setattr(cc_startup_process, "CCStreamSession", _FakeCCFails)
        monkeypatch.setattr(
            cc_destination, "_main_working_dir", lambda pid, db: str(tmp_path)
        )
        # The stale-cwd respawn gate resolves the session's default via the same
        # _main_working_dir; keep this fixture's working-dir override consistent
        # across both seams (as production is — one function) so a freshly
        # cold-started session is not misread as stale and respawned (#841).
        monkeypatch.setattr(
            cc_feed_transition, "_main_working_dir", lambda pid, db: str(tmp_path)
        )

        with pytest.raises(RuntimeError, match="simulated feed failure"):
            await feed_main_with_persisted_input(
                "sm-fail", "task text", target=cc_target("opus"),
                binary="/fake/binary",
                claude_dir=str(tmp_path / "no-claude-home"),
                broadcast=lambda s, m: None,
            )
        rt._LIVE.pop("sm-fail", None)

        reader = make_conn()
        roles = [r["role"] for r in reader.execute(
            "SELECT role FROM messages WHERE thread_id = 'sm-fail'"
        ).fetchall()]
        reader.close()

        assert "system" in roles, (
            "the seed row is committed at cold-start, before the first feed — a "
            "failing first feed() must not lose it (durability guard)."
        )


class TestCCSessionProvidedConnection:
    """The CC registry writer declares, rather than steals, supplied ownership."""

    @staticmethod
    def _insert_thread(conn, thread_id: str) -> None:
        conn.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
            "VALUES (?, 'u1', 'claude-code/opus', 't', 't')",
            (thread_id,),
        )

    def test_clean_supplied_connection_commits_its_cc_unit(self, _cold_start_db):
        from registry.listing import find_for_thread
        from registry.service import register_cc_session

        make_conn = _cold_start_db
        connection = make_conn()
        self._insert_thread(connection, "clean-owner")
        connection.commit()

        register_cc_session("clean-owner", "clean-provider", db=connection)

        assert connection.in_transaction is False
        reader = make_conn()
        try:
            current = find_for_thread(
                "clean-owner", "claude-code", "session", db=reader,
            )
        finally:
            reader.close()
            connection.close()
        assert current is not None
        assert current["external_id"] == "clean-provider"

    def test_nested_supplied_owner_uses_savepoint_without_flushing_work(
        self, _cold_start_db,
    ):
        from registry.listing import find_for_thread
        from registry.service import register_cc_session

        make_conn = _cold_start_db
        connection = make_conn()
        self._insert_thread(connection, "nested-owner")
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        self._insert_thread(connection, "unrelated-pending")

        register_cc_session("nested-owner", "nested-provider", db=connection)

        assert connection.in_transaction is True
        reader = make_conn()
        try:
            assert reader.execute(
                "SELECT id FROM threads WHERE id='unrelated-pending'"
            ).fetchone() is None
            assert find_for_thread(
                "nested-owner", "claude-code", "session", db=reader,
            ) is None
        finally:
            reader.close()

        connection.commit()
        reader = make_conn()
        try:
            current = find_for_thread(
                "nested-owner", "claude-code", "session", db=reader,
            )
        finally:
            reader.close()
            connection.close()
        assert current is not None
        assert current["external_id"] == "nested-provider"
