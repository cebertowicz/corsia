"""The conductor's first turn on a new install (#3287, OB10).

The first account queues one wake on the workspace conductor; it waits for the
user's first model choice, and the landing reads ``first_run_pending`` until a
durable handoff exists. Every test runs against a real database.
"""

from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

import asyncio
import json
import sqlite3
import re
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from tests.plan_writer_authorizer import raw_plan_seed

SECRET_ENOUGH = "long-enough-passphrase"
SETUP_TITLES = [
    "Getting started",
    "Additional provider setup",
    "Configuration checks",
    "Connect web search",
    "Set up GitHub",
    "Remote access",
    "Set up your first project",
]
SERVER = {"source": "claude-desktop", "app": "Claude Desktop", "servers": [{"name": "a", "kind": "stdio"}]}


@pytest.fixture(autouse=True)
def importable_tools(monkeypatch):
    """The install finds MCP servers and no session folder; no test reads the real home folder."""
    monkeypatch.setattr("tools.mcp_import.detected_sources", lambda *a, **k: [SERVER])
    monkeypatch.setattr("onboarding.project_discovery.session_apps", lambda _roots: [])

FIRST_RUN = (
    Path(__file__).resolve().parents[2]
    / "backend/prompts/system/v1/skills/conductor-bootstrap/references/first-run.md"
)


@pytest.fixture
def fresh_install(isolated_db):
    """The isolated database with no accounts, as a new install starts."""
    from infra.database import write_transaction

    with write_transaction(None, scope="test_fresh_install") as db:
        db.execute("DELETE FROM auth_tokens")
        db.execute("DELETE FROM users")
    return isolated_db


@pytest.fixture
def events(monkeypatch):
    """Capture onboarding events instead of writing analytics."""
    from infra import onboarding_events

    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(onboarding_events, "_emitted", set())
    monkeypatch.setattr(onboarding_events, "analytics_disabled", lambda: False)
    monkeypatch.setattr(
        onboarding_events, "_already_emitted",
        lambda action: action in onboarding_events._emitted,
    )
    def capture(category, action, properties=None, **_k):
        if category == "onboarding":
            captured.append((action, properties or {}))
            return True
        return False

    monkeypatch.setattr(onboarding_events, "emit_product_event", capture)
    return captured


def _conductor() -> str:
    from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator

    conductor_id = get_corsia_agent()
    assert conductor_id
    return conductor_id


def _rows(sql: str, *args) -> list[dict]:
    from infra.database import get_db

    db = get_db()
    try:
        return [dict(row) for row in db.execute(sql, args).fetchall()]
    finally:
        db.close()


def _first_run_rows() -> list[dict]:
    return _rows("SELECT * FROM messages WHERE id LIKE 'first-run:%'")


def _pending(conductor_id: str) -> bool:
    from infra.database import get_db
    from infra.onboarding_events import first_run_pending

    db = get_db()
    try:
        return first_run_pending(conductor_id, db=db)
    finally:
        db.close()


def _insert(thread_id: str, role: str, message_id: str, metadata: dict | None = None):
    from infra.database import write_transaction
    from infra.queries import insert_message

    if role == "assistant":
        metadata = {"turn_id": f"{message_id}-turn", **(metadata or {})}
    with write_transaction(None, scope="test_insert") as db:
        insert_message(thread_id, role, "text", metadata or {}, msg_id=message_id, db=db)


def _mark_update(thread_id: str, message_id: str):
    from infra.database import write_transaction
    from infra.queries import insert_message
    from infra.thread_updates import record_marked
    from infra.time import utc_now_iso

    with write_transaction(None, scope="test_mark_update") as db:
        created_at = utc_now_iso()
        insert_message(
            thread_id, "assistant", "A durable report", {"turn_id": f"{message_id}-turn"},
            msg_id=message_id, created_at=created_at, db=db,
        )
        record_marked(
            db, thread_id=thread_id, message_id=message_id, node_id=None,
            created_at=created_at,
        )


class TestWake:
    def test_first_account_queues_one_held_wake(self, fresh_install, events):
        from infra import accounts
        from infra.onboarding_events import FIRST_RUN_CHIP, first_run_message_id

        accounts.create_user("newcomer", SECRET_ENOUGH)

        conductor_id = _conductor()
        [row] = _first_run_rows()
        assert row["id"] == first_run_message_id(conductor_id)
        assert (row["thread_id"], row["role"]) == (conductor_id, "system")
        assert row["delivery_error"] == "no_model_selected"
        assert "conductor-bootstrap" in row["content"]
        assert "references/first-run.md" in row["content"]
        assert "Details:" not in row["content"]
        metadata = json.loads(row["metadata"])
        assert metadata["summary"] == FIRST_RUN_CHIP
        # No model is picked yet, so the chip must not say the conductor is already starting.
        assert FIRST_RUN_CHIP == "CORSIA was installed. Pick a model to start your conductor."
        assert "reference" not in json.dumps(metadata.get("details") or {})
        assert [action for action, _ in events] == ["account_created", "first_run_wake"]

    def test_a_system_recorded_before_the_account_is_no_news_to_its_conductor(self, fresh_install, monkeypatch):
        """A System recorded before the account existed is no news to a conductor never run."""
        from infra import accounts, bulletin_live, bulletins
        from infra.onboarding_events import first_run_message_id

        with monkeypatch.context() as patch:
            earlier = bulletins.SystemIdentity("0", "earlier", (("a", "0"),))
            patch.setattr(bulletins, "system_identity", lambda: earlier)
            asyncio.run(bulletin_live.check_live_bulletins())
        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = _conductor()

        assert asyncio.run(bulletin_live.check_live_bulletins()) is False
        rows = _rows(
            "SELECT id, delivery_error FROM messages WHERE thread_id=? ORDER BY rowid",
            conductor_id,
        )
        ids = [row["id"] for row in rows]
        assert first_run_message_id(conductor_id) in ids
        assert not [i for i in ids if i.startswith("system-change")]
        assert {row["delivery_error"] for row in rows} == {"no_model_selected"}

    def test_a_second_account_gets_no_wake(self, fresh_install, events):
        from infra import accounts

        accounts.create_user("newcomer", SECRET_ENOUGH)
        accounts.create_user("second", SECRET_ENOUGH)

        assert len(_first_run_rows()) == 1
        assert [action for action, _ in events] == ["account_created", "first_run_wake"]


class TestWakeAndSetupPlan:
    def test_prompt_references_only_seeded_tasks_and_defines_none(self):
        from infra.onboarding_events import FIRST_RUN_SETUP_TASKS

        first_run = FIRST_RUN.read_text(encoding="utf-8")
        plan_first = first_run.split("**Workers.**")[0]
        seeded = dict(FIRST_RUN_SETUP_TASKS)

        quoted_in_plan_first = set(re.findall(r"`([^`]+)`", plan_first))
        titles_in_plan_first = {q for q in quoted_in_plan_first if q[:1].isupper()}
        # v0.3.0 seeded task 7 under its old title; a setup still open across
        # the upgrade renames it rather than closing it as no longer part of setup.
        legacy_project = "Set up your first agent or project"
        assert titles_in_plan_first == set(seeded) | {legacy_project}
        assert f"`{legacy_project}` is task 7: `edit` its title to `Set up your first project`" in plan_first
        assert "`Getting started`" in first_run
        assert "operation=add" not in plan_first
        for criteria in seeded.values():
            assert criteria not in first_run

    def test_first_account_seeds_the_visible_setup_plan(self, fresh_install):
        from infra import accounts

        accounts.create_user("newcomer", SECRET_ENOUGH)

        conductor_id = _conductor()
        rows = _rows(
            """SELECT n.title, n.acceptance_criteria, n.state
               FROM plan_nodes n JOIN plans p ON p.id = n.plan_id
               WHERE p.owner_kind='thread' AND p.owner_id=? AND n.archived_at IS NULL
               ORDER BY n.order_key""",
            conductor_id,
        )
        from infra.onboarding_events import FIRST_RUN_SETUP_TASKS

        from infra.onboarding_events import TASK_START

        # "Getting started" is already in progress; the rest wait their turn.
        assert rows == [
            {
                "title": title, "acceptance_criteria": criteria,
                "state": "active" if title == TASK_START else "pending",
            }
            for title, criteria in FIRST_RUN_SETUP_TASKS
        ]
        assert [row["title"] for row in rows] == SETUP_TITLES

    def test_app_path_without_the_served_reference_uses_script_setup(self, fresh_install, monkeypatch):
        from infra import accounts, onboarding_events
        from infra.database import get_db

        started = Mock()
        monkeypatch.setattr(onboarding_events, "start_first_run_task", started)
        monkeypatch.setattr("onboarding.first_run_wake.app_reference_served", lambda: False)
        accounts.create_first_account("newcomer", SECRET_ENOUGH, first_run_path="app")

        [wake] = _first_run_rows()
        assert json.loads(wake["metadata"])["first_run_path"] == "script"
        # The path is decided once at creation: serving the reference later does not flip it.
        monkeypatch.setattr("onboarding.first_run_wake.app_reference_served", lambda: True)
        db = get_db()
        try:
            assert onboarding_events.held_first_run_path(_conductor(), db=db) == "script"
        finally:
            db.close()
        assert [row["title"] for row in _rows(
            """SELECT n.title FROM plan_nodes n JOIN plans p ON p.id = n.plan_id
               WHERE p.owner_kind='thread' AND p.owner_id=? AND n.archived_at IS NULL ORDER BY n.order_key""",
            _conductor(),
        )] == SETUP_TITLES
        started.assert_called_once()

    def test_served_app_path_seeds_no_setup_tasks_or_start(self, fresh_install, monkeypatch):
        from infra import accounts, onboarding_events

        started = Mock()
        monkeypatch.setattr(onboarding_events, "start_first_run_task", started)
        monkeypatch.setattr("onboarding.first_run_wake.app_reference_served", lambda: True)
        accounts.create_first_account("newcomer", SECRET_ENOUGH, first_run_path="app")

        [wake] = _first_run_rows()
        assert json.loads(wake["metadata"])["first_run_path"] == "app"
        assert _rows("SELECT n.title FROM plan_nodes n JOIN plans p ON p.id = n.plan_id WHERE p.owner_id=?", _conductor()) == []
        started.assert_not_called()

    def test_the_seeded_tasks_do_not_depend_on_what_the_install_finds(
        self, fresh_install, monkeypatch,
    ):
        from infra import accounts

        monkeypatch.setattr("tools.mcp_import.detected_sources", lambda *a, **k: [])
        accounts.create_user("newcomer", SECRET_ENOUGH)

        rows = _rows(
            """SELECT n.title FROM plan_nodes n JOIN plans p ON p.id = n.plan_id
               WHERE p.owner_kind='thread' AND p.owner_id=? AND n.archived_at IS NULL
               ORDER BY n.order_key""",
            _conductor(),
        )
        assert [row["title"] for row in rows] == SETUP_TITLES

    def test_a_session_folder_alone_seeds_the_same_tasks(self, fresh_install, monkeypatch):
        from infra import accounts

        monkeypatch.setattr("tools.mcp_import.detected_sources", lambda *a, **k: [])
        monkeypatch.setattr("onboarding.project_discovery.session_apps", lambda _roots: ["Codex"])
        accounts.create_user("newcomer", SECRET_ENOUGH)

        rows = _rows(
            """SELECT n.title FROM plan_nodes n JOIN plans p ON p.id = n.plan_id
               WHERE p.owner_kind='thread' AND p.owner_id=? AND n.archived_at IS NULL
               ORDER BY n.order_key""",
            _conductor(),
        )
        assert [row["title"] for row in rows] == SETUP_TITLES

    def test_setup_decisions_apply_only_while_a_seeded_task_is_open(self, fresh_install):
        from infra import accounts
        from infra.database import write_transaction
        from onboarding.setup_decisions import has_open_setup_task

        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = _conductor()
        assert has_open_setup_task(conductor_id) is True
        assert has_open_setup_task("no-such-thread") is False

        with write_transaction(None, scope="test_settle_setup_tasks") as db:
            with raw_plan_seed(db):
                db.execute(
                    "UPDATE plan_nodes SET state = 'accepted' WHERE plan_id IN "
                    "(SELECT id FROM plans WHERE owner_id = ?)", (conductor_id,))
        assert has_open_setup_task(conductor_id) is False

        from infra.database import get_db
        from plans import public

        db = get_db()
        try:
            assert public.has_open_titled_node(db, thread_id=conductor_id, titles=[]) is False
            assert public.has_open_titled_node(
                db, thread_id=conductor_id, titles=["Getting started"]) is False
        finally:
            db.close()

    def test_missing_conductor_does_not_rollback_first_account(self, fresh_install, monkeypatch):
        from infra import accounts, conductor
        from registry import shared_ownership_assignment

        monkeypatch.setattr(conductor, "ensure_project_conductor", Mock(return_value=None))
        monkeypatch.setattr(shared_ownership_assignment, "enforce_scaffold_ownership", Mock())

        assert accounts.create_user("newcomer", SECRET_ENOUGH) == "newcomer"

    def test_second_account_does_not_duplicate_the_setup_plan(self, fresh_install):
        from infra import accounts

        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = _conductor()
        accounts.create_user("second", SECRET_ENOUGH)

        rows = _rows(
            """SELECT n.title FROM plan_nodes n JOIN plans p ON p.id = n.plan_id
               WHERE p.owner_kind='thread' AND p.owner_id=? AND n.archived_at IS NULL
               ORDER BY n.order_key""",
            conductor_id,
        )
        assert [row["title"] for row in rows] == SETUP_TITLES

    def test_replaying_setup_seeding_does_not_duplicate_the_plan(self, fresh_install):
        from infra import accounts
        from infra.database import write_transaction
        from infra.onboarding_events import seed_first_run_setup_plan

        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = _conductor()
        with write_transaction(None, scope="test_replay_setup_seeding") as db:
            seed_first_run_setup_plan(conductor_id, "newcomer", db=db)

        rows = _rows(
            """SELECT n.title FROM plan_nodes n JOIN plans p ON p.id = n.plan_id
               WHERE p.owner_kind='thread' AND p.owner_id=? AND n.archived_at IS NULL
               ORDER BY n.order_key""",
            conductor_id,
        )
        assert [row["title"] for row in rows] == SETUP_TITLES

    def test_a_retry_is_a_duplicate(self, fresh_install):
        from infra import accounts
        from infra.database import write_transaction
        from infra.onboarding_events import enqueue_first_run_wake

        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = _conductor()
        with write_transaction(None, scope="test_retry") as db:
            assert enqueue_first_run_wake(conductor_id, db=db) is False
        assert len(_first_run_rows()) == 1

    def test_a_conductor_with_history_is_left_alone(self, fresh_install):
        from infra import accounts
        from infra.database import write_transaction
        from infra.onboarding_events import enqueue_first_run_wake

        accounts.create_user("newcomer", SECRET_ENOUGH, first_run_wake=False)
        conductor_id = _conductor()
        _insert(conductor_id, "user", "earlier")
        with write_transaction(None, scope="test_history") as db:
            assert enqueue_first_run_wake(conductor_id, db=db) is False
        assert _first_run_rows() == []

    def test_disabled_first_run_wake_seeds_no_setup_plan(self, fresh_install):
        from infra import accounts

        accounts.create_user("newcomer", SECRET_ENOUGH, first_run_wake=False)
        rows = _rows(
            """SELECT n.title FROM plan_nodes n JOIN plans p ON p.id = n.plan_id
               WHERE p.owner_kind='thread' AND p.owner_id=? AND n.archived_at IS NULL""",
            _conductor(),
        )

        assert rows == []

    def test_the_eval_harness_opts_out(self, fresh_install, events):
        from eval.credentials import mint_disposable_token

        mint_disposable_token("eval")

        assert _conductor()
        assert _first_run_rows() == []
        assert [action for action, _ in events] == ["account_created"]

    @pytest.mark.parametrize("error", [sqlite3.OperationalError("database is locked"), RuntimeError("boom")])
    def test_a_failed_first_run_task_start_never_breaks_account_creation(
        self, fresh_install, events, monkeypatch, error,
    ):
        from infra import accounts
        from plans import public

        monkeypatch.setattr(public, "mutate", Mock(side_effect=error))

        assert accounts.create_user("newcomer", SECRET_ENOUGH) == "newcomer"

        assert [action for action, _ in events] == ["account_created", "first_run_wake"]
        assert len(_first_run_rows()) == 1

    def test_a_failed_emit_never_breaks_account_creation(self, fresh_install, monkeypatch):
        from infra import accounts, product_events

        monkeypatch.setattr(product_events, "analytics_disabled", lambda: False)
        failing_open = Mock(side_effect=RuntimeError("analytics down"))
        monkeypatch.setattr(product_events.analytics_db, "get_db", failing_open)
        assert accounts.create_user("newcomer", SECRET_ENOUGH) == "newcomer"
        assert len(_first_run_rows()) == 1
        assert failing_open.called


@pytest.mark.asyncio
async def test_the_first_model_choice_drives_the_held_wake(fresh_install, events, monkeypatch):
    import infra.thread_execution as execution
    from infra import accounts
    from thread.model import switch_model

    drive = AsyncMock()
    monkeypatch.setattr(execution, "drive_thread", drive)
    monkeypatch.setattr("infra.state.broadcast_to_thread", Mock())
    accounts.create_user("newcomer", SECRET_ENOUGH)
    conductor_id = _conductor()

    outcome = await switch_model(
        conductor_id, "claude-code/sonnet", user_id="newcomer", actor_kind="user",
        actor_id="newcomer", reason="harness_model_switch",
    )

    assert outcome.changed is True
    [row] = _first_run_rows()
    assert (row["delivery_state"], row["delivery_error"], row["target_model"]) == (
        "queued", None, "claude-code/sonnet")
    drive.assert_awaited_once_with(conductor_id)
    [(action, properties)] = [e for e in events if e[0] == "first_model_selected"]
    assert properties["source_kind"] == "claude-code"
    assert isinstance(properties["seconds_since_account"], int)


class TestPending:
    def test_an_assistant_row_without_canonical_contact_stays_pending(self, fresh_install):
        from infra import accounts

        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = _conductor()
        assert _pending(conductor_id) is True

        _insert(conductor_id, "assistant", "reply")
        assert _pending(conductor_id) is True

    def test_post_wake_marked_update_survives_reader_reopen_with_a_queued_wake(self, fresh_install):
        from infra import accounts

        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = _conductor()
        [wake] = _first_run_rows()
        assert wake["seq"] is None

        _mark_update(conductor_id, "report")
        assert _pending(conductor_id) is False

    def test_public_decision_handoff_ends_pending(self, fresh_install, monkeypatch):
        from infra import accounts
        from plans import public

        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = _conductor()
        calls = []

        def raised(db, *, owner_thread_id, boundary):
            calls.append((db, owner_thread_id, boundary))
            return True

        monkeypatch.setattr(public, "has_genuine_decision_raised_at_or_after", raised)
        assert _pending(conductor_id) is False
        assert calls[0][1] == conductor_id

    def test_post_wake_read_error_fails_open_without_writing(self, fresh_install, monkeypatch, caplog):
        import sqlite3

        from infra import accounts, onboarding_events
        from infra.database import get_db
        from plans import public

        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = _conductor()
        monkeypatch.setattr(
            public, "has_genuine_decision_raised_at_or_after",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                sqlite3.DatabaseError("private candidate content"),
            ),
        )
        caplog.set_level("WARNING", logger=onboarding_events.__name__)
        db = get_db()
        try:
            before = db.total_changes
            assert onboarding_events.first_run_pending(conductor_id, db=db) is True
            assert db.total_changes == before
        finally:
            db.close()
        assert "DatabaseError" in caplog.text
        assert "private candidate content" not in caplog.text

    def test_malformed_wake_timestamp_stays_pending(self):
        from infra.onboarding_events import first_run_pending

        class MalformedWakeRead:
            def execute(self, _sql, _params):
                return self

            def fetchone(self):
                return {"created_at": "malformed"}

        assert first_run_pending("conductor", db=MalformedWakeRead()) is True

    def test_plans_programming_error_propagates(self, fresh_install, monkeypatch):
        from infra import accounts
        from plans import public

        accounts.create_user("newcomer", SECRET_ENOUGH)
        monkeypatch.setattr(
            public,
            "has_genuine_decision_raised_at_or_after",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(TypeError("programming defect")),
        )

        with pytest.raises(TypeError, match="programming defect"):
            _pending(_conductor())

    @pytest.mark.parametrize(
        ("rows", "expected"),
        [
            (["2026-10-03T00:59:59-07:00"], False),
            (["2026-10-03T01:00:00-07:00"], True),
            (["2026-10-03T08:00:00Z"], True),
            (["2026-10-03 08:00:00"], True),
            ([None, "malformed", "2026-10-03T00:59:59-07:00", "2026-10-03 08:00:00"], True),
        ],
    )
    def test_marked_handoff_compares_parsed_instants(self, rows, expected):
        from datetime import datetime

        from infra.onboarding_events import _has_marked_handoff

        class CandidateRows:
            def execute(self, _sql, _params):
                return [{"created_at": value} for value in rows]

        assert _has_marked_handoff(
            "conductor", datetime(2026, 10, 3, 8), db=CandidateRows(),
        ) is expected

    def test_wake_read_error_propagates(self):
        import sqlite3

        from infra.onboarding_events import first_run_pending

        class BrokenWakeRead:
            def execute(self, _sql, _params):
                raise sqlite3.DatabaseError("wake unreadable")

        with pytest.raises(sqlite3.DatabaseError, match="wake unreadable"):
            first_run_pending("conductor", db=BrokenWakeRead())

    def test_an_errored_first_turn_stays_pending(self, fresh_install):
        from infra import accounts

        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = _conductor()
        _insert(conductor_id, "system", "failure", {"type": "llm_error"})
        assert _pending(conductor_id) is True

    def test_no_wake_means_not_pending(self, fresh_install):
        from infra import accounts

        accounts.create_user("newcomer", SECRET_ENOUGH, first_run_wake=False)
        assert _pending(_conductor()) is False

    @pytest.mark.asyncio
    async def test_the_corsia_agent_route_reports_it_and_a_project_route_never_does(self, fresh_install, monkeypatch):
        from api import conductor as conductor_routes
        from api import corsia_agent as corsia_agent_routes
        from infra import accounts
        from infra.conductor import set_project_conductor

        async def truth(*_a, **_k):
            return {}

        monkeypatch.setattr(conductor_routes, "read_runtime_truth", truth)
        monkeypatch.setattr(conductor_routes, "authenticate", lambda _t: {"id": "newcomer"})
        monkeypatch.setattr(corsia_agent_routes, "authenticate", lambda _t: {"id": "newcomer"})
        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = _conductor()

        body = await corsia_agent_routes.get_corsia_agent_route(token="caller")
        assert body["thread_id"] == conductor_id
        assert body["first_run_pending"] is True

        _mark_update(conductor_id, "report")
        body = await corsia_agent_routes.get_corsia_agent_route(token="[REDACTED]")
        assert body["first_run_pending"] is False

        from infra.database import write_transaction

        with write_transaction(None, scope="test_project") as db:
            db.execute("INSERT INTO projects (id,name,path,created_at,updated_at)"
                       " VALUES ('proj-a','A','/a','t','t')")
        assert set_project_conductor("proj-a", conductor_id) is True
        body = await conductor_routes.get_conductor_route(token="caller", scope="proj-a")
        assert body["thread_id"] == conductor_id
        assert body["first_run_pending"] is False


class TestModelSources:
    def _seed_endpoint(self):
        from llm import network_discovery as nd
        from infra.database import write_transaction

        sid = nd.upsert_server("external", "lab-inst", "Lab GPU", "127.0.0.1", 9000,
                               "online", kind="external")
        nd.set_server_metadata(sid, api_key="sk-live-secret-value")
        with write_transaction(None, scope="test_endpoint_model") as db:
            db.execute(
                "INSERT INTO models (id, provider, provider_model_id, litellm_model,"
                " display_name, enabled, available, created_at, updated_at)"
                " VALUES (?,?,?,?,?,1,1,'t','t')",
                (f"external@{sid}/m", "external", "m", f"external@{sid}/m", "m"),
            )
        return sid

    @pytest.mark.asyncio
    async def test_shape_github_and_no_credentials(self, isolated_db, monkeypatch):
        from connections.models import ConnectionType, upsert_connection
        from tools.model_sources import ModelSourcesTool

        sid = self._seed_endpoint()
        upsert_connection(type=ConnectionType.CLI, provider="github",
                          config={"binary": "/bin/gh", "login": "octo"}, status="available")

        body = json.loads(await ModelSourcesTool().execute())

        assert set(body) == {"sources", "github", "github_agent"}
        assert body["github"] == {"state": "ready"}
        assert "github" not in {s["source"] for s in body["sources"]}
        assert f"external:{sid}" in {s["source"] for s in body["sources"]}
        text = json.dumps(body).lower()
        for word in ("sk-live", "api_key", "token", "credential", "password"):
            assert word not in text

    @pytest.mark.parametrize(("status", "state"), [
        ("needs_auth", "needs_auth"), ("not_installed", "not_installed"),
        ("error", "unverified"),
    ])
    def test_github_states(self, isolated_db, status, state):
        from connections.models import ConnectionType, upsert_connection
        from tools.model_sources import github_state

        upsert_connection(type=ConnectionType.CLI, provider="github",
                          config={"login": "octo"}, status=status)
        assert github_state() == {"state": state}

    def test_no_github_row_is_not_installed(self, isolated_db):
        from tools.model_sources import github_state

        assert github_state() == {"state": "not_installed"}

    @pytest.mark.asyncio
    async def test_recheck_runs_the_checks_once_for_concurrent_callers(self, isolated_db, monkeypatch):
        import asyncio

        from api import provider_sources
        from tools.model_sources import ModelSourcesTool

        runs = 0
        gate = asyncio.Event()

        async def checks():
            nonlocal runs
            runs += 1
            await gate.wait()

        monkeypatch.setattr(provider_sources, "_inflight", None)
        monkeypatch.setattr(provider_sources, "_recheck", checks)
        tool = ModelSourcesTool()
        first = asyncio.create_task(tool.execute(recheck=True))
        second = asyncio.create_task(provider_sources.recheck_sources_once())
        await asyncio.sleep(0)
        gate.set()
        body = json.loads(await first)
        await second

        assert runs == 1
        assert {s["source"] for s in body["sources"]} >= {"claude-code", "codex", "omlx"}

    @pytest.mark.asyncio
    async def test_a_plain_read_runs_no_check(self, isolated_db, monkeypatch):
        from api import provider_sources
        from tools.model_sources import ModelSourcesTool

        checks = AsyncMock()
        monkeypatch.setattr(provider_sources, "_recheck", checks)
        await ModelSourcesTool().execute()
        checks.assert_not_awaited()

    def test_registered_for_agents(self):
        from tools.catalog_entries import TOOL_CATALOG

        assert TOOL_CATALOG["model_sources"].category == "projects"


class TestEvents:
    def test_unreadable_analytics_is_not_cached_and_a_later_emit_persists_once(
        self, fresh_install, monkeypatch,
    ):
        from infra import analytics_db, onboarding_events

        action = "first_turn_completed"
        monkeypatch.setattr(onboarding_events, "_emitted", set())
        monkeypatch.setattr(onboarding_events, "analytics_disabled", lambda: False)
        monkeypatch.setattr(
            analytics_db, "get_db", Mock(side_effect=OSError("analytics unavailable")),
        )
        assert onboarding_events._already_emitted(action) is None
        assert action not in onboarding_events._emitted

        class MissingEvent:
            def execute(self, *_args, **_kwargs):
                return self

            def fetchone(self):
                return None

        monkeypatch.setattr(analytics_db, "get_db", MissingEvent)
        persisted = Mock(side_effect=[False, True])
        monkeypatch.setattr(onboarding_events, "emit_product_event", persisted)
        onboarding_events._emit_first(action, source_kind="test")
        assert action not in onboarding_events._emitted
        onboarding_events._emit_first(action, source_kind="test")
        assert action in onboarding_events._emitted
        onboarding_events._emit_first(action, source_kind="test")
        assert persisted.call_count == 2

    def test_real_analytics_failures_recover_without_duplicate_emission(
        self, fresh_install, monkeypatch, tmp_path,
    ):
        from infra import analytics_db, config, onboarding_events

        path = tmp_path / "analytics.duckdb"
        monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
        monkeypatch.setattr(config, "ANALYTICS_DB_PATH", path)
        monkeypatch.setattr(analytics_db, "ANALYTICS_DB_PATH", path)
        analytics_db._reset_for_tests()
        monkeypatch.setattr(onboarding_events, "_emitted", set())
        real_get_db = analytics_db.get_db

        def count(action):
            db = real_get_db()
            return db.execute(
                "SELECT COUNT(*) FROM product_events WHERE category=? AND action=?",
                (onboarding_events.CATEGORY, action),
            ).fetchone()[0]

        class ReadFailure:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, *args, **kwargs):
                if str(sql).lstrip().startswith("SELECT"):
                    raise OSError("analytics read unavailable")
                return self.connection.execute(sql, *args, **kwargs)

        class InsertFailure:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, *args, **kwargs):
                if str(sql).lstrip().startswith("INSERT"):
                    raise OSError("analytics insert unavailable")
                return self.connection.execute(sql, *args, **kwargs)

        try:
            open_action = "real_open_failure"
            monkeypatch.setattr(
                analytics_db, "get_db",
                Mock(side_effect=OSError("analytics open unavailable")),
            )
            onboarding_events._emit_first(open_action)
            assert open_action not in onboarding_events._emitted
            monkeypatch.setattr(analytics_db, "get_db", real_get_db)
            onboarding_events._emit_first(open_action)
            assert count(open_action) == 1
            onboarding_events._emitted.clear()
            onboarding_events._emit_first(open_action)
            assert count(open_action) == 1

            read_action = "real_read_failure"
            onboarding_events._emit_first(read_action)
            assert count(read_action) == 1
            onboarding_events._emitted.clear()
            monkeypatch.setattr(
                analytics_db, "get_db", lambda: ReadFailure(real_get_db()),
            )
            onboarding_events._emit_first(read_action)
            assert read_action not in onboarding_events._emitted
            monkeypatch.setattr(analytics_db, "get_db", real_get_db)
            onboarding_events._emit_first(read_action)
            assert count(read_action) == 1

            missing_read_action = "real_missing_read_failure"
            monkeypatch.setattr(
                analytics_db, "get_db", lambda: ReadFailure(real_get_db()),
            )
            onboarding_events._emit_first(missing_read_action)
            assert missing_read_action not in onboarding_events._emitted
            monkeypatch.setattr(analytics_db, "get_db", real_get_db)
            assert count(missing_read_action) == 0
            onboarding_events._emit_first(missing_read_action)
            assert count(missing_read_action) == 1
            onboarding_events._emitted.clear()
            onboarding_events._emit_first(missing_read_action)
            assert count(missing_read_action) == 1

            insert_action = "real_insert_failure"
            monkeypatch.setattr(
                analytics_db, "get_db", lambda: InsertFailure(real_get_db()),
            )
            onboarding_events._emit_first(insert_action)
            assert insert_action not in onboarding_events._emitted
            monkeypatch.setattr(analytics_db, "get_db", real_get_db)
            assert count(insert_action) == 0
            onboarding_events._emit_first(insert_action)
            assert count(insert_action) == 1
            onboarding_events._emitted.clear()
            onboarding_events._emit_first(insert_action)
            assert count(insert_action) == 1
        finally:
            analytics_db._reset_for_tests()

    def test_persisted_event_dedupes_after_cache_reset(
        self, fresh_install, monkeypatch,
    ):
        from infra import analytics_db, onboarding_events

        class PersistedEvent:
            def execute(self, *_args, **_kwargs):
                return self

            def fetchone(self):
                return {"present": 1}

        action = "first_turn_completed"
        monkeypatch.setattr(onboarding_events, "_emitted", set())
        monkeypatch.setattr(analytics_db, "get_db", PersistedEvent)
        assert onboarding_events._already_emitted(action) is True
        onboarding_events._emitted.clear()
        assert onboarding_events._already_emitted(action) is True

    def test_turn_completed_only_for_the_workspace_conductor(self, fresh_install, events):
        from infra import accounts
        from infra.onboarding_events import note_turn_completed

        accounts.create_user("newcomer", SECRET_ENOUGH)
        note_turn_completed("some-other-thread")
        assert "first_turn_completed" not in [a for a, _ in events]

        note_turn_completed(_conductor())
        note_turn_completed(_conductor())
        assert [a for a, _ in events].count("first_turn_completed") == 1

    def test_decision_answer_uses_the_plans_owner_read(
        self, fresh_install, events, monkeypatch,
    ):
        from infra import accounts
        from infra.onboarding_events import note_decision_answered
        from plans import public

        accounts.create_user("newcomer", SECRET_ENOUGH)
        conductor_id = _conductor()
        owner_read = Mock(return_value=conductor_id)
        monkeypatch.setattr(public, "decision_owner_id", owner_read)

        note_decision_answered("decision-node", "A")

        owner_read.assert_called_once()
        [(action, properties)] = [
            event for event in events if event[0] == "first_decision_answered"
        ]
        assert (action, properties) == ("first_decision_answered", {"option": "A"})

    def test_first_outcome_needs_an_accepted_close_in_a_new_project(self, fresh_install, events):
        from infra import accounts
        from infra.database import write_transaction
        from infra.onboarding_events import note_close

        accounts.create_user("newcomer", SECRET_ENOUGH)
        with write_transaction(None, scope="test_projects") as db:
            db.execute("INSERT INTO projects (id,name,path,created_at,updated_at)"
                       " VALUES ('old','Old','/old','2000-01-01T00:00:00Z','t')")
            db.execute("INSERT INTO projects (id,name,path,created_at,updated_at)"
                       " VALUES ('new','New','/new','2999-01-01T00:00:00Z','t')")

        note_close("request_review", "new")
        note_close("accept", "old")
        note_close("mark_done", None)
        assert "first_outcome" not in [a for a, _ in events]

        note_close("mark_done", "new")
        [(_, properties)] = [e for e in events if e[0] == "first_outcome"]
        assert properties["operation"] == "mark_done"

    def test_properties_are_content_free(self, fresh_install, events):
        from infra import accounts
        from infra.onboarding_events import note_first_model_selected

        accounts.create_user("newcomer", SECRET_ENOUGH)
        note_first_model_selected(_conductor(), "external@srv-1/secret-model-name")
        [(_, properties)] = [e for e in events if e[0] == "first_model_selected"]
        assert properties["source_kind"] == "external"
        assert "secret" not in json.dumps(properties)

    def test_time_to_first_outcome_sql(self):
        import duckdb

        from infra.onboarding_events import TIME_TO_FIRST_OUTCOME_SQL

        con = duckdb.connect(":memory:")
        con.execute("CREATE TABLE product_events (ts TIMESTAMP, category VARCHAR,"
                    " action VARCHAR, properties JSON, session_id VARCHAR, project_id VARCHAR)")
        assert con.execute(TIME_TO_FIRST_OUTCOME_SQL).fetchone() == (None,)
        con.execute("INSERT INTO product_events VALUES"
                    " ('2026-10-01 10:00:00','onboarding','account_created',NULL,NULL,NULL),"
                    " ('2026-10-01 10:12:30','onboarding','first_outcome',NULL,NULL,NULL),"
                    " ('2026-10-01 11:00:00','onboarding','first_outcome',NULL,NULL,NULL)")
        assert con.execute(TIME_TO_FIRST_OUTCOME_SQL).fetchone() == (750,)


class TestProjection:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from core import system_projection

        system_projection._CACHE.clear()
        yield
        system_projection._CACHE.clear()

    def test_first_run_reference_is_projected_with_its_manifest_digest(self, tmp_path):
        import hashlib

        from core.system_projection import ensure_system_projection
        from infra.system_package import system_package_dir

        workspace = tmp_path / "Corsia"
        projection = ensure_system_projection(workspace)

        assert projection is not None
        relative = "skills/conductor-bootstrap/references/first-run.md"
        manifest = json.loads((system_package_dir() / "MANIFEST.json").read_text())
        [entry] = [f for f in manifest["files"] if f["path"] == relative]
        projected = projection / relative
        assert hashlib.sha256(projected.read_bytes()).hexdigest() == entry["sha256"]

    def test_workspace_conductor_skills_index_lists_the_trigger(self, fresh_install, tmp_path):
        from core.skills_registry import skill_context_sources
        from infra import accounts
        from infra.database import get_db

        accounts.create_user("newcomer", SECRET_ENOUGH)
        db = get_db()
        try:
            sources = skill_context_sources(
                thread_id=_conductor(), project_id=None, workspace=tmp_path / "Corsia", db=db,
            )
        finally:
            db.close()
        [index] = [s for s in sources if s.source_id == "dynamic:skills-index"]
        [line] = [l for l in index.prompt_content.splitlines()
                  if l.startswith("- conductor-bootstrap:")]
        assert "a new install's first run" in line
