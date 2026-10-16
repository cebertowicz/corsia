"""Conductor designation routes over a self-contained sqlite fixture."""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

from infra.schema import STRUCTURED_PLAN_SCHEMA_SQL
from infra.checkpoint_schema import CHECKPOINT_SCHEMA_SQL
from tests.registry_helpers import ARTIFACT_REGISTRY_DDL

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


class _NoClose:
    def __init__(self, c):
        self._c = c

    def execute(self, *a):
        return self._c.execute(*a)

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()

    @property
    def in_transaction(self):
        return self._c.in_transaction

    def close(self):
        pass


@pytest.fixture
def conductor_db(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "conductor.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT,
            model TEXT, bootloader_files TEXT,
            reasoning_effort TEXT,
            project_id TEXT, parent_thread_id TEXT, spawned_by TEXT,
            lifecycle_state TEXT DEFAULT 'active', dormant_at TEXT,
            archived_at TEXT, archived_reason TEXT, purged_at TEXT,
            created_at TEXT, updated_at TEXT,
            runtime_context_epoch INTEGER NOT NULL DEFAULT 0,
            next_message_seq INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, metadata TEXT, created_at TEXT NOT NULL,
            seq INTEGER
        );
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT,
            conductor_thread_id TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """ + ARTIFACT_REGISTRY_DDL
    )
    conn.executescript(STRUCTURED_PLAN_SCHEMA_SQL + CHECKPOINT_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO projects (id, name, path, created_at, updated_at) "
        "VALUES ('proj-a', 'Proj A', '/Users/x/proj-a', 't', 't')"
    )
    conn.commit()
    wrapper = _NoClose(conn)

    import infra.conductor as conductor_mod
    import infra.database as database
    import infra.settings as settings_mod
    from registry import models as registry_models
    from subagents import child_thread

    factory = lambda: wrapper  # noqa: E731
    monkeypatch.setattr(child_thread, "get_db", factory)
    monkeypatch.setattr(registry_models, "get_db", factory)
    monkeypatch.setattr(conductor_mod, "get_db", factory)
    monkeypatch.setattr(database, "get_db", factory)
    monkeypatch.setattr(settings_mod, "get_db", factory)

    yield wrapper
    conn.close()


class TestConductorRoutes:
    @pytest.fixture
    def inline_db(self, monkeypatch):
        async def _inline(fn, *a, **kw):
            return fn(*a, **kw)

        import api.conductor as conductor_routes

        def authenticate(token: str) -> dict[str, str] | None:
            if token == "tok":
                return {"id": "steve", "token_kind": "session"}
            if token == "tok2":
                return {"id": "leksi"}
            return None

        monkeypatch.setattr(conductor_routes, "run_in_db", _inline)
        monkeypatch.setattr(conductor_routes, "run_read", _inline)
        monkeypatch.setattr(conductor_routes, "authenticate", authenticate)
        return _inline

    @pytest.mark.asyncio
    async def test_conductor_routes(self, inline_db, conductor_db, monkeypatch):
        from fastapi import HTTPException
        import api.conductor as conductor_routes
        from llm.resolution import ExecutionTarget

        monkeypatch.setattr(
            "infra.runtime_truth.resolve_execution_target",
            lambda model: ExecutionTarget(
                catalog_id=model,
                harness="native",
                provider="vmlx",
                backend="api",
                transport_model=model,
                expected_reported_model=None,
                context_window=200_000,
                available=True,
                availability_reason=None,
                display_name=model,
                supports_reasoning=False,
            ),
        )

        import infra.queries as queries_mod

        monkeypatch.setattr(queries_mod, "get_db", lambda: conductor_db)
        conductor_db.execute(
            "INSERT INTO threads (id, user_id, title, model, "
            "reasoning_effort, lifecycle_state, bootloader_files) VALUES "
            "('sess-1', 'steve', 'Native conductor', 'vmlx-local-qwen', 'max', 'active', "
            "'[\"context/CONDUCTOR.md\"]')"
        )
        conductor_db.execute(
            "INSERT INTO threads (id, user_id, lifecycle_state, archived_reason) "
            "VALUES ('sess-arch', 'steve', 'archived', 'user')"
        )
        conductor_db.commit()

        out = await conductor_routes.put_conductor_route(
            token="tok", body={"thread_id": "sess-1"}, scope="proj-a"
        )
        assert out["ok"] is True
        got = await conductor_routes.get_conductor_route(token="tok", scope="proj-a")
        assert got["thread_id"] == "sess-1"
        # The target carries the row's own server time, the value the thread
        # list would order this thread's model by.
        row_updated_at = conductor_db.execute(
            "SELECT updated_at FROM threads WHERE id = 'sess-1'"
        ).fetchone()["updated_at"]
        assert row_updated_at
        assert got["target"] == {
            "thread_id": "sess-1",
            "title": "Native conductor",
            "setup_incomplete": False,
            "bootloader_files": '["context/CONDUCTOR.md"]',
            "updated_at": row_updated_at,
            "model": "vmlx-local-qwen",
            "reasoning_effort": "max",
            "thread_kind": "chat",
            "project_id": None,
            "driver": "engine",
            "execution_class": "native",
            "cc_live": False,
            "cc_has_session": False,
            "codex_live": False,
            "codex_provider_context_untrusted": False,
            "codex_has_thread": False,
            "codex_has_prior_messages": False,
            "busy": False,
            "busy_reason": None,
            # "no turn is running" and "the last turns ran and produced
            # nothing" are different answers; a fresh thread is plainly idle.
            "liveness": "idle",
            "unproductive_turns": 0,
            "queued_inputs": 0,
            "last_turn": None,
            "addressable": True,
            "addressability_error": None,
            "conductor_scopes": [
                {"scope": "proj-a", "scope_type": "project", "project_name": "Proj A"}
            ],
        }

        await conductor_routes.put_conductor_route(
            token="tok", body={"thread_id": "sess-arch"}, scope="proj-a"
        )
        got = await conductor_routes.get_conductor_route(token="tok", scope="proj-a")
        assert got["thread_id"] is None
        assert got["target"] is None

        conductor_db.execute("DELETE FROM threads WHERE id = 'sess-1'")
        conductor_db.execute(
            "UPDATE projects SET conductor_thread_id = 'sess-1' WHERE id = 'proj-a'"
        )
        conductor_db.commit()
        got = await conductor_routes.get_conductor_route(token="tok", scope="proj-a")
        assert got["thread_id"] is None

        with pytest.raises(HTTPException) as e:
            await conductor_routes.put_conductor_route(
                token="tok", body={"thread_id": "missing"}, scope="proj-a"
            )
        assert e.value.status_code == 404

        with pytest.raises(HTTPException) as e:
            await conductor_routes.put_conductor_route(
                token="tok", body={"thread_id": "sess-arch"}, scope="ghost"
            )
        assert e.value.status_code == 400

    @pytest.mark.asyncio
    async def test_conductor_get_hides_private_target_from_nonowner(
        self, inline_db, conductor_db, monkeypatch,
    ):
        from fastapi import HTTPException
        import api.conductor as conductor_routes
        import api.thread_messages as message_routes
        import infra.queries as queries_mod

        async def runtime_truth(*_args, **_kwargs):
            return {}

        authenticate = (
            lambda t: {"id": "steve", "token_kind": "session"}
            if t == "tok"
            else {"id": "leksi"}
            if t == "tok2"
            else None
        )
        monkeypatch.setattr(queries_mod, "get_db", lambda: conductor_db)
        monkeypatch.setattr(message_routes, "authenticate", authenticate)
        monkeypatch.setattr(message_routes, "run_in_db", inline_db)
        monkeypatch.setattr(message_routes, "run_read", inline_db)
        monkeypatch.setattr(message_routes, "get_db", lambda: conductor_db)
        monkeypatch.setattr(conductor_routes, "read_runtime_truth", runtime_truth)
        conductor_db.execute(
            "INSERT INTO threads (id, user_id, title, model, lifecycle_state) "
            "VALUES ('owner-conductor', 'steve', 'Owner conductor', 'm', 'active')"
        )
        conductor_db.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at, seq) "
            "VALUES ('msg-1', 'owner-conductor', 'user', 'private', 't', 1)"
        )
        conductor_db.commit()

        await conductor_routes.put_conductor_route(
            token="tok", body={"thread_id": "owner-conductor"}, scope="proj-a",
        )
        owner = await conductor_routes.get_conductor_route(token="tok", scope="proj-a")
        assert owner["thread_id"] == "owner-conductor"
        assert owner["target"]["thread_id"] == "owner-conductor"

        other = await conductor_routes.get_conductor_route(token="tok2", scope="proj-a")
        assert other == {"scope": "proj-a", "thread_id": None, "target": None,
                         "first_run_pending": False}
        with pytest.raises(HTTPException) as exc:
            await message_routes.get_messages("owner-conductor", token="tok2")
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_removed_model_keeps_conductor_target_readable(
        self, inline_db, conductor_db, monkeypatch,
    ):
        import api.conductor as conductor_routes
        import infra.queries as queries_mod
        from llm.resolution import ModelUnavailableError

        removed_model = "omlx@local/qwen-flash-next"
        conductor_db.execute(
            "INSERT INTO threads (id, user_id, title, model, lifecycle_state) "
            "VALUES ('stale-conductor', 'steve', 'Stale conductor', ?, 'active')",
            (removed_model,),
        )
        conductor_db.commit()
        monkeypatch.setattr(queries_mod, "get_db", lambda: conductor_db)

        def missing_model(model):
            raise ModelUnavailableError(model, f"Model '{model}' not found in catalog")

        monkeypatch.setattr(
            "infra.runtime_truth.resolve_execution_target", missing_model,
        )
        await conductor_routes.put_conductor_route(
            token="tok", body={"thread_id": "stale-conductor"}, scope="proj-a",
        )
        try:
            result = await conductor_routes.get_conductor_route(
                token="tok", scope="proj-a",
            )
        except ModelUnavailableError as exc:
            pytest.fail(f"conductor status raised for a removed model: {exc}")

        target = result["target"]
        assert target["model"] == removed_model
        assert target["driver"] is None
        assert target["execution_class"] is None
        assert target["addressable"] is False
        assert target["addressability_error"] == (
            f"Model '{removed_model}' not found in catalog. Choose another model."
        )


class TestWorkspaceScopeIsTheCorsiaAgent:
    """The workspace scope moved to /api/corsia-agent; the old route 404s loudly."""

    @pytest.fixture
    def routes(self, conductor_db, monkeypatch):
        async def _inline(fn, *a, **kw):
            return fn(*a, **kw)

        import api.conductor as conductor_routes
        import api.corsia_agent as corsia_agent_routes
        import infra.corsia_agent as corsia_agent_mod
        import infra.queries as queries_mod

        def authenticate(_credential):
            return {"id": "steve", "token_kind": "session"}

        async def truth(*_a, **_k):
            return {}

        monkeypatch.setattr(conductor_routes, "run_in_db", _inline)
        monkeypatch.setattr(conductor_routes, "run_read", _inline)
        monkeypatch.setattr(conductor_routes, "authenticate", authenticate)
        monkeypatch.setattr(conductor_routes, "read_runtime_truth", truth)
        monkeypatch.setattr(corsia_agent_routes, "authenticate", authenticate)
        monkeypatch.setattr(queries_mod, "get_db", lambda: conductor_db)
        monkeypatch.setattr(corsia_agent_mod, "get_db", lambda: conductor_db)
        from infra.context_change_schema import CONTEXT_CHANGE_SCHEMA_SQL

        conductor_db._c.executescript(CONTEXT_CHANGE_SCHEMA_SQL)
        conductor_db.execute(
            "INSERT INTO threads (id, user_id, title, lifecycle_state) "
            "VALUES ('agent-1', 'steve', 'CORSIA', 'active')"
        )
        conductor_db.commit()
        return conductor_routes, corsia_agent_routes

    @pytest.mark.asyncio
    async def test_old_route_404s_for_the_workspace_scope(self, routes):
        from fastapi import HTTPException

        conductor_routes, _ = routes
        with pytest.raises(HTTPException) as read:
            await conductor_routes.get_conductor_route("session", scope="workspace")
        with pytest.raises(HTTPException) as write:
            await conductor_routes.put_conductor_route(
                "session", {"thread_id": "agent-1"}, scope="workspace"
            )
        assert read.value.status_code == 404
        assert write.value.status_code == 404
        assert "/api/corsia-agent" in read.value.detail

    @pytest.mark.asyncio
    async def test_corsia_agent_route_designates_and_reads(self, routes, conductor_db):
        _, corsia_agent_routes = routes
        out = await corsia_agent_routes.put_corsia_agent_route(
            "session", {"thread_id": "agent-1"}
        )
        assert out == {"ok": True, "scope": "workspace", "thread_id": "agent-1"}
        got = await corsia_agent_routes.get_corsia_agent_route("session")
        assert got["scope"] == "workspace"
        assert got["thread_id"] == "agent-1"
        rows = conductor_db.execute("SELECT key FROM settings").fetchall()
        assert [row["key"] for row in rows] == ["corsia_agent_thread_id"]

    @pytest.mark.asyncio
    async def test_old_route_still_serves_project_scopes(self, routes):
        conductor_routes, _ = routes
        got = await conductor_routes.get_conductor_route("session", scope="proj-a")
        assert got["scope"] == "proj-a"
        assert got["thread_id"] is None
