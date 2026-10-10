
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from infra.schema import SCHEMA_SQL


class _NoClose:
    def __init__(self, c):
        self._c = c

    def execute(self, *a):
        return self._c.execute(*a)

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()

    def __getattr__(self, name):
        return getattr(self._c, name)

    def close(self):
        self._c.rollback()


@pytest.fixture
def delete_db(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "delete.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    wrapper = _NoClose(conn)

    import api.thread_deletion as deletion_mod
    import api.thread_mutations as sessions_mod
    import infra.database as database
    from registry import models as registry_models
    from subagents import cc_runtime, child_thread, codex_runtime

    # These runtime modules retain imported database helpers. Load them before
    # replacing the canonical writer seam so one fixture cannot leak its closed
    # connection factory into the next test.
    _ = cc_runtime, codex_runtime
    factory = lambda: wrapper  # noqa: E731
    monkeypatch.setattr(sessions_mod, "get_db", factory)
    monkeypatch.setattr(database, "get_db", factory)
    monkeypatch.setattr(registry_models, "get_db", factory)
    monkeypatch.setattr(child_thread, "get_db", factory)

    async def _inline(fn, *a, **kw):
        return fn(*a, **kw)

    monkeypatch.setattr(sessions_mod, "run_in_db", _inline)
    monkeypatch.setattr(deletion_mod, "run_in_db", _inline)

    # Retiring a harness runtime now moves its SQLite writes off the event loop
    # through ``infra.db_async.run_in_db``. This fixture isolates DB access to
    # one connection created here, so run that work on this thread too: several
    # modules bind ``run_in_db`` at import time, and the shared pool is the one
    # seam that reaches all of them.
    from delegation_fixtures import _InlineExecutor
    from infra import db_async

    for pool in ("_pool", "_read_pool", "_bulk_pool"):
        monkeypatch.setattr(db_async, pool, _InlineExecutor())
    monkeypatch.setattr(sessions_mod, "authenticate",
                        lambda t: {"id": "steve"} if t == "tok" else None)
    monkeypatch.setattr(sessions_mod, "emit_product_event",
                        lambda *a, **kw: None)

    yield wrapper
    conn.close()


def test_patch_and_delete_session_routes_register_once():
    from main import app

    counts = {"PATCH": 0, "DELETE": 0}
    for included in app.routes:
        router = getattr(included, "original_router", None)
        for route in router.routes if router else (included,):
            if getattr(route, "path", None) != "/api/threads/{thread_id}":
                continue
            for method in counts.keys() & (getattr(route, "methods", None) or set()):
                counts[method] += 1
    assert counts == {"PATCH": 1, "DELETE": 1}


class TestDeleteFreesCCSession:
    @pytest.mark.asyncio
    async def test_delete_stops_a_live_cc_process(self, delete_db, monkeypatch):
        import api.thread_mutations as sessions_mod
        from subagents import cc_runtime

        delete_db.execute(
            "INSERT INTO threads (id, user_id, created_at, updated_at) "
            "VALUES ('s-live', 'steve', 't', 't')")
        delete_db.commit()

        retired = []
        pending = True

        async def _retire(sid, **kw):
            nonlocal pending
            retired.append(sid)
            pending = False
            return True

        monkeypatch.setattr(
            cc_runtime,
            "has_pending_or_live",
            lambda sid: sid == "s-live" and pending,
        )
        monkeypatch.setattr(cc_runtime, "retire", _retire)
        await sessions_mod.delete_thread("s-live", token="tok")
        assert retired == ["s-live"]

    @pytest.mark.asyncio
    async def test_delete_cascades_thread_rows(self, delete_db):
        import api.thread_mutations as sessions_mod

        delete_db.executescript(
            """
            CREATE TABLE deleted_epoch_audit (
                thread_id TEXT NOT NULL, history_epoch INTEGER NOT NULL
            );
            CREATE TRIGGER audit_deleted_session_epoch
            AFTER UPDATE OF history_epoch ON threads
            WHEN NEW.history_epoch != OLD.history_epoch
            BEGIN
                INSERT INTO deleted_epoch_audit (thread_id, history_epoch)
                VALUES (NEW.id, NEW.history_epoch);
            END;
            """
        )
        delete_db.execute(
            "INSERT INTO threads (id, user_id, created_at, updated_at) "
            "VALUES ('s-1', 'steve', 't', 't')")
        delete_db.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at) "
            "VALUES ('m-1', 's-1', 'user', 'hi', 't')")

        delete_db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
            "VALUES ('turn-s-1','s-1',1,'native/test','completed','t','boot')"
        )
        delete_db.commit()

        await sessions_mod.delete_thread("s-1", token="tok")
        for table, col in [
            ("threads", "id"),
            ("messages", "thread_id"),
            ("active_turns", "thread_id"),
        ]:
            assert delete_db.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE {col} = 's-1'"
            ).fetchone()["c"] == 0

        assert tuple(delete_db.execute(
            "SELECT thread_id, history_epoch FROM deleted_epoch_audit"
        ).fetchone()) == ("s-1", 1)

    @pytest.mark.asyncio
    async def test_deleted_owner_can_be_claimed_on_next_register(
            self, delete_db):
        import api.thread_mutations as sessions_mod
        from registry import models

        delete_db.execute(
            "INSERT INTO threads (id, user_id, created_at, updated_at) "
            "VALUES ('owner-delete', 'steve', 't', 't')"
        )
        delete_db.execute(
            "INSERT INTO threads (id, user_id, created_at, updated_at) "
            "VALUES ('subscriber', 'steve', 't', 't')"
        )
        first = models.register_artifact(
            integration="workspace",
            type="document",
            external_id="/workspace/shared.md",
            parent_thread_id="owner-delete",
            db=delete_db,
        )
        models.register_artifact(
            integration="workspace",
            type="document",
            external_id="/workspace/shared.md",
            parent_thread_id="subscriber",
            db=delete_db,
        )
        delete_db.commit()

        await sessions_mod.delete_thread("owner-delete", token="tok")

        assert delete_db.execute(
            "SELECT COUNT(*) c FROM artifact_registrations "
            "WHERE parent_thread_id='owner-delete'"
        ).fetchone()["c"] == 0
        assert delete_db.execute(
            "SELECT COUNT(*) c FROM artifact_registration_events "
            "WHERE artifact_id=? AND event_type='ownership_released'",
            (first["id"],),
        ).fetchone()["c"] == 0

        claimed = models.register_artifact(
            integration="workspace",
            type="document",
            external_id="/workspace/shared.md",
            parent_thread_id="subscriber",
            db=delete_db,
        )
        assert claimed["parent_thread_id"] == "subscriber"
        assert delete_db.execute(
            "SELECT relationship_state FROM artifact_registrations "
            "WHERE artifact_id=? AND parent_thread_id='subscriber'",
            (first["id"],),
        ).fetchone()["relationship_state"] == "owner-active"

    @pytest.mark.asyncio
    async def test_delete_retires_owned_ephemeral_without_retiring_durable_or_subscribed(
            self, delete_db):
        import api.thread_mutations as sessions_mod
        from registry import models

        for thread_id in ("owner-delete", "other-owner", "replacement"):
            delete_db.execute(
                "INSERT INTO threads (id, user_id, created_at, updated_at) "
                "VALUES (?, 'steve', 't', 't')",
                (thread_id,),
            )
        owned_tmp = models.register_artifact(
            integration="upload",
            type="document",
            external_id="upload://temporary",
            parent_thread_id="owner-delete",
            retention="ephemeral",
            db=delete_db,
        )
        owned_durable = models.register_artifact(
            integration="workspace",
            type="document",
            external_id="workspace://durable",
            parent_thread_id="owner-delete",
            retention="durable",
            db=delete_db,
        )
        shared = models.register_artifact(
            integration="workspace",
            type="document",
            external_id="workspace://shared",
            parent_thread_id="other-owner",
            retention="durable",
            db=delete_db,
        )
        models.register_artifact(
            integration="workspace",
            type="document",
            external_id="workspace://shared",
            parent_thread_id="owner-delete",
            retention="ephemeral",
            db=delete_db,
        )
        delete_db.commit()

        await sessions_mod.delete_thread("owner-delete", token="tok")

        assert {
            row["id"]: row["state"]
            for row in delete_db.execute(
                "SELECT id,state FROM artifact_identities "
                "WHERE id IN (?,?,?) ORDER BY id",
                (owned_tmp["id"], owned_durable["id"], shared["id"]),
            ).fetchall()
        } == {
            owned_durable["id"]: "active",
            owned_tmp["id"]: "archived",
            shared["id"]: "active",
        }
        assert delete_db.execute(
            "SELECT COUNT(*) c FROM artifact_registrations "
            "WHERE parent_thread_id='owner-delete'"
        ).fetchone()["c"] == 0

        reactivated = models.register_artifact(
            integration="upload",
            type="document",
            external_id="upload://temporary",
            parent_thread_id="replacement",
            retention="ephemeral",
            db=delete_db,
        )
        assert reactivated["id"] == owned_tmp["id"]
        assert reactivated["state"] == "active"
        assert reactivated["parent_thread_id"] == "replacement"

    @pytest.mark.asyncio
    async def test_unknown_or_foreign_session_404s_without_stopping(
            self, delete_db, monkeypatch):
        from fastapi import HTTPException
        import api.thread_mutations as sessions_mod
        from subagents import cc_runtime, codex_runtime

        monkeypatch.setattr(
            cc_runtime,
            "has_pending_or_live",
            lambda sid: pytest.fail("must not probe before 404"),
        )
        monkeypatch.setattr(
            codex_runtime,
            "retire",
            lambda sid, **_kw: pytest.fail("must not retire before 404"),
        )
        with pytest.raises(HTTPException) as e:
            await sessions_mod.delete_thread("ghost", token="tok")
        assert e.value.status_code == 404


class TestDeleteWakesDelegatingParent:
    @pytest.mark.asyncio
    async def test_hard_delete_wakes_the_delegating_parent(self, delete_db):
        import api.thread_mutations as sessions_mod
        from plans import service

        delete_db.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
            "VALUES ('parent', 'steve', 'native/test', 't', 't')")
        delete_db.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
            "VALUES ('worker', 'steve', 'native/test', 't', 't')")
        delete_db.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) "
            "VALUES ('project-1', 'Project', 't', 't')")
        delete_db.commit()

        parent_plan = service.ensure_plan("thread", "parent", db=delete_db)
        node = service.add_node(
            parent_plan.id, kind="task", title="Delegate work",
            acceptance_criteria="Worker result is reviewed",
            spawn_kind="claude-code", db=delete_db,
        )
        service.transition_node(node.id, "active", db=delete_db)
        delete_db.execute(
            "UPDATE plan_nodes SET spawned_thread_id='worker' WHERE id=?",
            (node.id,),
        )
        delete_db.commit()

        auth_token = "".join(["t", "o", "k"])
        await sessions_mod.delete_thread("worker", token=auth_token)

        wake = delete_db.execute(
            "SELECT content FROM messages WHERE thread_id='parent' "
            "AND id LIKE 'plan-wake:%'"
        ).fetchone()
        assert wake is not None
        assert "deleted" in str(wake["content"]).lower()
        refreshed = delete_db.execute(
            "SELECT spawned_thread_id FROM plan_nodes WHERE id=?", (node.id,),
        ).fetchone()
        assert refreshed["spawned_thread_id"] is None
