"""App-path first-run opening and setup-plan seed coverage."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest


PASSWORD = "long-enough-passphrase"


@pytest.fixture
def fresh_install(isolated_db):
    from infra.database import write_transaction

    with write_transaction(None, scope="fresh_app_install") as db:
        db.execute("DELETE FROM auth_tokens")
        db.execute("DELETE FROM users")
    return isolated_db


@pytest.fixture
def app_seed_facts(monkeypatch):
    from onboarding import app_first_run

    facts = {
        "agents_found": ["openclaw"],
        "connected_app_count": 0,
        "email_calendar_ready": False,
    }
    monkeypatch.setattr(app_first_run, "_pre_pick_facts", lambda *, db: facts)
    return facts


def _conductor_id() -> str:
    from infra.corsia_agent import get_corsia_agent

    conductor_id = get_corsia_agent()
    assert conductor_id
    return conductor_id


def _rows(sql: str, *args):
    from infra.database import get_db

    db = get_db()
    try:
        return [dict(row) for row in db.execute(sql, args).fetchall()]
    finally:
        db.close()


def _completed_app_facts() -> dict:
    """The completion snapshot shape, before a model has been selected."""
    return {
        "first_run_path": "app", "harness_ready": [], "sources_missing": {},
        "agents_found": ["openclaw"], "local_download": {}, "tool_attempts": None,
        "pays_for": [], "web_search_ready": False, "github_ready": False,
        "setup": {"setup_decisions": []}, "user_profile": {"name": None, "full_name": None},
        "connected_app_count": 0, "email_calendar_ready": False,
    }


def test_app_account_holds_only_the_neutral_wake_until_the_first_model_pick(
    fresh_install, app_seed_facts,
):
    from infra import accounts, config, user_profile
    from infra.database import write_transaction
    from infra.onboarding_events import first_run_message_id
    from onboarding.first_run_opening import seed_first_model_opening

    accounts.create_first_account(
        "newcomer", PASSWORD, full_name="  New\x01   Comer ", first_run_path="app",
    )

    conductor_id = _conductor_id()
    assert user_profile.profile_name(config.WORKSPACE) == "New"
    assert _rows("SELECT * FROM messages WHERE id=?", f"first-run-opening:{conductor_id}") == []
    assert _rows("SELECT id FROM plan_nodes WHERE workflow_ref='first-run-app'")
    [wake] = _rows("SELECT * FROM messages WHERE id=?", first_run_message_id(conductor_id))
    assert (wake["role"], wake["delivery_state"], wake["delivery_error"]) == (
        "system", "queued", "no_model_selected",
    )
    [thread] = _rows("SELECT model FROM threads WHERE id=?", conductor_id)
    assert thread["model"] is None

    with write_transaction(None, scope="seed_app_first_model") as db:
        seed_first_model_opening(conductor_id, db=db)
        seed_first_model_opening(conductor_id, db=db)

    [opening] = _rows("SELECT * FROM messages WHERE id=?", f"first-run-opening:{conductor_id}")
    assert (opening["thread_id"], opening["role"], opening["delivery_state"]) == (
        conductor_id, "assistant", "delivered",
    )
    assert opening["content"].startswith("Hi New.\n\nI'm CORSIA.")
    metadata = json.loads(opening["metadata"])
    assert metadata["for_user"] is True
    assert metadata["turn_unbound_reason"] == "server_scripted"
    assert "turn_id" not in metadata
    assert len(_rows("SELECT * FROM messages WHERE id=?", opening["id"])) == 1
    assert _rows("SELECT id FROM plan_nodes WHERE workflow_ref='first-run-app'")


def test_first_model_transaction_writes_the_opening_and_model_once(
    fresh_install, app_seed_facts, monkeypatch,
):
    from connections import account_selection
    from infra import accounts
    from thread.model_row import select_first_model
    import llm.resolution as resolution

    accounts.create_first_account("newcomer", PASSWORD, first_run_path="app")
    conductor_id = _conductor_id()
    monkeypatch.setattr(resolution, "resolve_execution_target", lambda *_args, **_kwargs: type(
        "Target", (), {"available": True, "availability_reason": None},
    )())
    monkeypatch.setattr(account_selection, "bind_model_account", lambda *_args, **_kwargs: None)

    assert select_first_model(
        conductor_id, "test/model", user_id="newcomer", now="t", first_run_wake="first turn",
    )
    assert _rows("SELECT model FROM threads WHERE id=?", conductor_id) == [{"model": "test/model"}]
    assert len(_rows("SELECT * FROM messages WHERE id=?", f"first-run-opening:{conductor_id}")) == 1
    assert _rows("SELECT id FROM plan_nodes WHERE workflow_ref='first-run-app'")
    assert select_first_model(
        conductor_id, "other/model", user_id="newcomer", now="later", first_run_wake="later turn",
    ) is False
    assert len(_rows("SELECT * FROM messages WHERE id=?", f"first-run-opening:{conductor_id}")) == 1


def test_first_model_transaction_rolls_back_the_opening_and_binding_on_seed_failure(
    fresh_install, app_seed_facts, monkeypatch,
):
    from connections import account_selection
    from infra import accounts
    from onboarding import first_run_opening
    from thread.model_row import select_first_model
    import llm.resolution as resolution

    accounts.create_first_account("newcomer", PASSWORD, first_run_path="app")
    conductor_id = _conductor_id()
    monkeypatch.setattr(resolution, "resolve_execution_target", lambda *_args, **_kwargs: type(
        "Target", (), {"available": True, "availability_reason": None},
    )())
    monkeypatch.setattr(account_selection, "bind_model_account", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(first_run_opening, "opening_message", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("seed failed"),
    ))

    with pytest.raises(RuntimeError, match="seed failed"):
        select_first_model(
            conductor_id, "test/model", user_id="newcomer", now="t", first_run_wake="first turn",
        )
    assert _rows("SELECT model FROM threads WHERE id=?", conductor_id) == [{"model": None}]
    assert _rows("SELECT id FROM messages WHERE id=?", f"first-run-opening:{conductor_id}") == []
    assert _rows("SELECT id FROM plan_nodes WHERE workflow_ref='first-run-app'")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_run_path", "consume_wake", "handled"),
    [("script", False, False), ("app", True, False), ("app", False, True)],
)
async def test_complete_app_setup_only_reads_facts_for_a_held_app_wake(
    fresh_install, app_seed_facts, monkeypatch, first_run_path, consume_wake, handled,
):
    from unittest.mock import AsyncMock

    from infra import accounts, setup_progress
    from infra.database import write_transaction
    from infra.onboarding_events import first_run_message_id
    from onboarding import app_first_run

    facts = {"agents_found": [], "connected_app_count": 0, "email_calendar_ready": False}
    read_facts = AsyncMock(return_value=facts)
    monkeypatch.setattr(app_first_run, "read_app_facts", read_facts)
    user_id = accounts.create_first_account(
        "newcomer", PASSWORD, full_name="New Comer", first_run_path=first_run_path,
    )
    conductor_id = _conductor_id()
    if consume_wake:
        with write_transaction(None, scope="consume_app_wake") as db:
            db.execute(
                "UPDATE messages SET delivery_state='delivered', delivery_error=NULL WHERE id=?",
                (first_run_message_id(conductor_id),),
            )

    assert await app_first_run.complete_app_setup(
        user_id, step=None, local_ai=None, picks=None, handoff=None,
    ) is handled

    if not handled:
        read_facts.assert_not_awaited()
        assert setup_progress.view()["setup_completed"] is (first_run_path == "script")
        return

    assert read_facts.await_count == 1
    assert app_first_run.read_cached_app_facts(conductor_id) == facts
    assert setup_progress.view()["setup_completed"] is True
    # A repeated completion cannot reopen setup or duplicate final seed work.
    assert await app_first_run.complete_app_setup(
        user_id, step=None, local_ai=None, picks=None, handoff=None,
    ) is True
    assert read_facts.await_count == 2
    assert app_first_run.read_cached_app_facts(conductor_id) == facts
    assert setup_progress.view()["setup_completed"] is True


@pytest.mark.parametrize("kinds, count, step_three", [
    (("approved", "awaiting", "expired"), 2, False),
    (("approved", "expired"), 1, True),
])
def test_connected_app_facts_count_every_app_but_an_expired_proposal(isolated_db, kinds, count, step_three):
    from connections.app_lifecycle import approve_proposal, create_pending_app
    from infra.database import get_db, write_transaction
    from onboarding.app_first_run import _connected_app_facts, applicable_steps

    reach = {"mode": "all", "project_ids": [], "outside_projects": False}
    for index, kind in enumerate(kinds):
        app = create_pending_app(f"App {index}", {}, reach, actor_thread_id="importer", db=get_db())
        if kind == "approved":
            assert approve_proposal(app["id"], db=get_db())["state"] == "disconnected"
        elif kind == "expired":
            with write_transaction(None, scope="age_connected_app_proposal") as db:
                db.execute(
                    "UPDATE connected_apps SET state_since='2000-01-01T00:00:00' WHERE id=?", (app["id"],),
                )

    facts = _connected_app_facts()

    assert facts["connected_app_count"] == count
    assert (3 in {step["step_number"] for step in applicable_steps(facts)}) is step_three


def test_portable_history_refuses_a_fabricated_turn_for_the_opening(fresh_install, app_seed_facts):
    from history.projection import render_portable_history
    from history.types import PortableHistoryError
    from infra import accounts
    from infra.database import get_db, write_transaction

    accounts.create_first_account("newcomer", PASSWORD, full_name="New Comer", first_run_path="app")
    conductor_id = _conductor_id()
    from onboarding.first_run_opening import seed_first_model_opening

    with write_transaction(None, scope="fabricated_opening_turn") as db:
        seed_first_model_opening(conductor_id, db=db)
        db.execute(
            "UPDATE messages SET metadata=? WHERE id=?",
            (json.dumps({"turn_id": "invented-turn"}), f"first-run-opening:{conductor_id}"),
        )

    db = get_db()
    try:
        with pytest.raises(PortableHistoryError, match="message names missing Turn 'invented-turn'"):
            render_portable_history(conductor_id, db=db)
    finally:
        db.close()


def test_first_model_seed_preserves_restored_conductor_history(fresh_install, app_seed_facts):
    from infra import accounts, onboarding_events
    from infra.database import write_transaction
    from onboarding.first_run_opening import seed_first_model_opening
    from infra.queries import insert_message

    accounts.create_first_account("newcomer", PASSWORD, full_name="New Comer", first_run_path="app")
    with write_transaction(None, scope="restored_history") as db:
        db.execute(
            "INSERT INTO threads (id, user_id, created_at, updated_at) VALUES ('restored', 'newcomer', 't', 't')"
        )
        insert_message("restored", "user", "Restored conversation", db=db)
        onboarding_events.enqueue_first_run_wake("restored", first_run_path="app", db=db)
        seed_first_model_opening("restored", db=db)

    assert [row["role"] for row in _rows("SELECT role FROM messages WHERE thread_id='restored'")] == ["user"]
    assert _rows("SELECT id FROM plans WHERE owner_id='restored'") == []


def test_real_history_still_refuses_another_first_run_wake(fresh_install, app_seed_facts):
    from infra import accounts, onboarding_events
    from infra.database import write_transaction
    from infra.onboarding_events import first_run_message_id
    from infra.queries import insert_message

    accounts.create_first_account("newcomer", PASSWORD, full_name="New Comer", first_run_path="app")
    conductor_id = _conductor_id()
    with write_transaction(None, scope="prior_real_history") as db:
        db.execute("DELETE FROM messages WHERE id=?", (first_run_message_id(conductor_id),))
        insert_message(conductor_id, "user", "Earlier real history", db=db)
        accepted = onboarding_events.enqueue_first_run_wake(conductor_id, first_run_path="app", db=db)
    assert accepted is False


def test_applicable_steps_and_completion_cache_reconcile_conditionals(
    fresh_install, app_seed_facts,
):
    from infra import accounts
    from infra.database import write_transaction
    from onboarding.app_first_run import applicable_steps, cache_completed_facts, read_cached_app_facts

    facts = {
        "sources_missing": {"claude_code": "needs_auth", "codex": "not_installed"},
        "pays_for": ["claude_code"],
        "agents_found": ["openclaw"],
        "web_search_ready": False,
        "connected_app_count": 2,
        "email_calendar_ready": True,
        "setup": {"setup_decisions": []},
    }
    steps = applicable_steps(facts)
    connections = next(step for step in applicable_steps({}) if step["step_number"] == 3)
    assert "or you chose not now" in connections["acceptance_criteria"]
    assert [(step["step_number"], step["title"], step.get("vendor")) for step in steps] == [
        (1, "Add your Claude subscription", "claude_code"),
        (2, "Bring over what your other tools know", None),
        (4, "Web search", None),
        (5, "About you", None),
        (7, "How CORSIA gets better", None),
        (8, "Your files, and when agents ask first", None),
        (9, "Hand over to your agents", None),
    ]
    assert [step["acceptance_criteria"] for step in steps] == [
        "you connected your Claude subscription, or chose not now",
        "you chose whether to bring over what your other tools know",
        "web search works, or you chose not now",
        "CORSIA knows enough to help, or you chose not now",
        "you chose whether to use or share community improvements, or chose not now",
        "CORSIA explained how file access and approval work",
        "CORSIA asked what you'd like help with next",
    ]

    accounts.create_first_account("newcomer", PASSWORD, full_name="New Comer", first_run_path="app")
    conductor_id = _conductor_id()
    with write_transaction(None, scope="complete_app_seed") as db:
        cache_completed_facts(conductor_id, "newcomer", facts, db=db)
        assert read_cached_app_facts(conductor_id, db=db) == facts
        assert db.execute("SELECT 1 FROM plan_nodes WHERE workflow_ref='first-run-app'").fetchone() is not None

    live = _rows(
        "SELECT n.title, n.workflow_ref, n.workflow_step, n.difficulty "
        "FROM plan_nodes n JOIN plans p ON p.id=n.plan_id "
        "WHERE p.owner_id=? AND n.workflow_ref='first-run-app' "
        "AND n.archived_at IS NULL ORDER BY n.order_key", conductor_id,
    )
    assert [node["title"] for node in live] == [step["title"] for step in steps]
    assert [(node["workflow_ref"], node["workflow_step"], node["difficulty"]) for node in live] == [
        ("first-run-app", f"{step['step_number']}:{step.get('vendor', 'common')}", "routine")
        for step in steps
    ]


@pytest.mark.asyncio
async def test_uncached_first_pick_reconciles_current_facts_before_rendering(fresh_install, app_seed_facts, monkeypatch):
    from infra import accounts
    from onboarding import app_first_run, first_run_wake

    accounts.create_first_account("newcomer", PASSWORD, first_run_path="app")
    conductor_id = _conductor_id()
    facts = {
        "agents_found": [],
        "connected_app_count": 2,
        "email_calendar_ready": True,
        "web_search_ready": False,
        "sources_missing": {},
        "pays_for": [],
        "setup": {"setup_decisions": []},
    }
    monkeypatch.setattr(app_first_run, "read_app_facts", AsyncMock(return_value=facts))

    wake = await first_run_wake.render_for("test/model", "app", thread_id=conductor_id)

    assert "4. **Web search" in wake
    live = _rows(
        "SELECT workflow_step FROM plan_nodes WHERE workflow_ref='first-run-app' "
        "AND archived_at IS NULL ORDER BY workflow_step"
    )
    assert {node["workflow_step"] for node in live} == {
        f"{step['step_number']}:{step.get('vendor', 'common')}"
        for step in app_first_run.applicable_steps(facts)
    }


@pytest.mark.asyncio
async def test_app_facts_omit_bound_model_before_the_person_picks_one(monkeypatch):
    from onboarding import first_run_wake

    monkeypatch.setattr(first_run_wake, "_read_facts", AsyncMock(return_value={
        "bound_model": None,
        "model_sources": {
            "sources": [], "github": {"state": "unavailable"},
            "github_agent": {"state": "unavailable"},
        },
        "agents_found": ["openclaw"], "local_download": {}, "web_search_ready": False,
        "setup_handoff": None, "setup_picks": None, "setup": {"setup_decisions": []},
        "user_profile": {"name": None, "full_name": None},
    }))

    facts = await first_run_wake._app_setup_facts("")

    assert "bound_model" not in facts


@pytest.mark.asyncio
async def test_cached_pick_archives_a_discovered_import_when_its_card_is_absent(
    fresh_install, app_seed_facts, monkeypatch,
):
    from infra import accounts
    from onboarding import app_first_run, first_run_wake, setup_decisions

    accounts.create_first_account("newcomer", PASSWORD, first_run_path="app")
    conductor_id = _conductor_id()
    cached = _completed_app_facts()
    monkeypatch.setattr(app_first_run, "read_app_facts", AsyncMock(return_value=cached))
    assert await app_first_run.complete_app_setup(
        "newcomer", step=None, local_ai=None, picks=None, handoff=None,
    ) is True
    assert _rows(
        "SELECT archived_at FROM plan_nodes WHERE workflow_ref='first-run-app' AND workflow_step='2:common'"
    ) == [{"archived_at": None}]
    monkeypatch.setattr(first_run_wake, "_bound_model", lambda model: {"model": model, "source": "omlx"})
    monkeypatch.setattr(setup_decisions, "import_card", lambda model, ready: None)

    wake = await first_run_wake.render_for("test/model", "app", thread_id=conductor_id)

    assert "2. **Bring over what your other tools know" not in wake
    assert "3. **Where your work lives" in wake
    assert _rows(
        "SELECT archived_at FROM plan_nodes WHERE workflow_ref='first-run-app' AND workflow_step='2:common'"
    )[0]["archived_at"] is not None
    assert _rows(
        "SELECT title FROM plan_nodes WHERE workflow_ref='first-run-app' AND archived_at IS NULL "
        "ORDER BY order_key LIMIT 1"
    ) == [{"title": "Where your work lives"}]


@pytest.mark.asyncio
async def test_cached_pick_with_unchanged_facts_has_no_public_plan_mutations(
    fresh_install, app_seed_facts, monkeypatch,
):
    from infra import accounts
    from onboarding import app_first_run, first_run_wake, setup_decisions
    from plans import public

    accounts.create_first_account("newcomer", PASSWORD, first_run_path="app")
    conductor_id = _conductor_id()
    cached = _completed_app_facts()
    monkeypatch.setattr(app_first_run, "read_app_facts", AsyncMock(return_value=cached))
    assert await app_first_run.complete_app_setup(
        "newcomer", step=None, local_ai=None, picks=None, handoff=None,
    ) is True
    [before] = _rows("SELECT revision FROM plans WHERE owner_id=?", conductor_id)
    mutations = []
    real_mutate = public.mutate

    def record_mutation(*args, **kwargs):
        mutations.append((args, kwargs))
        return real_mutate(*args, **kwargs)

    monkeypatch.setattr(public, "mutate", record_mutation)
    monkeypatch.setattr(first_run_wake, "_bound_model", lambda model: {"model": model, "source": "omlx"})
    monkeypatch.setattr(
        setup_decisions, "import_card",
        lambda model, ready: setup_decisions.ImportCard(("OpenClaw",), (), ""),
    )

    wake = await first_run_wake.render_for("test/model", "app", thread_id=conductor_id)

    [after] = _rows("SELECT revision FROM plans WHERE owner_id=?", conductor_id)
    assert "2. **Bring over what your other tools know" in wake
    assert mutations == []
    assert after["revision"] == before["revision"]


@pytest.mark.asyncio
async def test_cached_pick_with_an_import_card_keeps_import_open_and_first(
    fresh_install, app_seed_facts, monkeypatch,
):
    from infra import accounts
    from onboarding import app_first_run, first_run_wake, setup_decisions

    accounts.create_first_account("newcomer", PASSWORD, first_run_path="app")
    conductor_id = _conductor_id()
    cached = _completed_app_facts()
    monkeypatch.setattr(app_first_run, "read_app_facts", AsyncMock(return_value=cached))
    assert await app_first_run.complete_app_setup(
        "newcomer", step=None, local_ai=None, picks=None, handoff=None,
    ) is True
    monkeypatch.setattr(first_run_wake, "_bound_model", lambda model: {"model": model, "source": "omlx"})
    monkeypatch.setattr(
        setup_decisions, "import_card",
        lambda model, ready: setup_decisions.ImportCard(("OpenClaw",), (), ""),
    )

    wake = await first_run_wake.render_for("test/model", "app", thread_id=conductor_id)

    assert "2. **Bring over what your other tools know" in wake
    assert _rows(
        "SELECT title FROM plan_nodes WHERE workflow_ref='first-run-app' AND archived_at IS NULL "
        "ORDER BY order_key LIMIT 1"
    ) == [{"title": "Bring over what your other tools know"}]


@pytest.mark.asyncio
async def test_cached_pick_keeps_an_unavailable_setup_marker_and_the_import_step(
    fresh_install, app_seed_facts, monkeypatch,
):
    from infra import accounts
    from onboarding import app_first_run, first_run_wake, setup_decisions

    accounts.create_first_account("newcomer", PASSWORD, first_run_path="app")
    conductor_id = _conductor_id()
    marker = {"unavailable": first_run_wake.UNAVAILABLE_READS["setup"]}
    cached = {**_completed_app_facts(), "setup": marker}
    monkeypatch.setattr(app_first_run, "read_app_facts", AsyncMock(return_value=cached))
    assert await app_first_run.complete_app_setup(
        "newcomer", step=None, local_ai=None, picks=None, handoff=None,
    ) is True
    monkeypatch.setattr(first_run_wake, "_bound_model", lambda model: {"model": model, "source": "omlx"})

    def no_card(model, ready):
        raise AssertionError("an unavailable setup read must not gain an import card")

    monkeypatch.setattr(setup_decisions, "import_card", no_card)

    wake = await first_run_wake.render_for("test/model", "app", thread_id=conductor_id)

    rendered, _ = json.JSONDecoder().raw_decode(wake.split("## Setup facts\n\n", 1)[1])
    assert rendered["setup"] == marker
    assert "Bring over what your other tools know" in [
        row["title"] for row in _rows(
            "SELECT title FROM plan_nodes WHERE workflow_ref='first-run-app' AND archived_at IS NULL"
        )
    ]


def test_completion_retry_uses_the_latest_facts_at_the_first_model_pick(fresh_install, app_seed_facts):
    from infra import accounts
    from infra.database import write_transaction
    from onboarding.app_first_run import applicable_steps, cache_completed_facts
    from plans import public

    accounts.create_first_account("newcomer", PASSWORD, first_run_path="app")
    conductor_id = _conductor_id()
    initial = {"agents_found": [], "connected_app_count": 0, "web_search_ready": False}
    updated = {**initial, "connected_app_count": 2, "web_search_ready": True}
    with write_transaction(None, scope="retry_changed_setup") as db:
        cache_completed_facts(conductor_id, "newcomer", initial, db=db)
        cache_completed_facts(conductor_id, "newcomer", updated, db=db)
        listing = public.read_current_plan(
            actor=public.PlanActor(conductor_id, "newcomer", None), include_folded=True, db=db,
        )
    remaining = [node["title"] for node in listing["nodes"] if node.get("archived_at") is None]
    assert remaining == [step["title"] for step in applicable_steps(updated)]


def test_first_model_seed_is_idempotent_after_completion(fresh_install, app_seed_facts):
    from infra import accounts
    from infra.database import write_transaction
    from onboarding.app_first_run import cache_completed_facts

    accounts.create_first_account("newcomer", PASSWORD, first_run_path="app")
    conductor_id = _conductor_id()
    facts = {"agents_found": [], "connected_app_count": 0, "web_search_ready": False}
    with write_transaction(None, scope="stable_first_pick_seed") as db:
        cache_completed_facts(conductor_id, "newcomer", facts, db=db)
        initial_ids = [row["id"] for row in db.execute(
            "SELECT id FROM plan_nodes WHERE workflow_ref='first-run-app' ORDER BY id"
        )]
        from onboarding.first_run_opening import seed_first_model_opening

        seed_first_model_opening(conductor_id, db=db)
        seed_first_model_opening(conductor_id, db=db)
        assert [row["id"] for row in db.execute(
            "SELECT id FROM plan_nodes WHERE workflow_ref='first-run-app' ORDER BY id"
        )] == initial_ids


def test_named_apps_are_children_and_preserve_visible_onboarding_steps(fresh_install, app_seed_facts):
    from infra import accounts
    from infra.database import write_transaction
    from onboarding.app_first_run import applicable_steps, cache_completed_facts
    from plans import public

    accounts.create_first_account("newcomer", PASSWORD, first_run_path="app")
    conductor_id = _conductor_id()
    facts = {"agents_found": [], "connected_app_count": 0, "web_search_ready": False}
    actor = public.PlanActor(conductor_id, "newcomer", None)
    with write_transaction(None, scope="named_app_children") as db:
        cache_completed_facts(conductor_id, "newcomer", facts, db=db)
        listing = public.read_current_plan(actor=actor, include_folded=True, db=db)
        connect = next(node for node in listing["nodes"] if node["title"] == "Where your work lives")
        assert "each connection is finished or skipped" in public.get_node(connect["id"], db=db).acceptance_criteria
        for app in ("Slack", "Notes"):
            public.add_node(
                listing["plan"]["id"], kind="task", title=f"Connect {app}",
                parent_id=connect["id"], acceptance_criteria="Connected, or the user chose not now",
                provenance="test.app_first_run", db=db,
            )
        final = public.read_current_plan(actor=actor, include_folded=True, db=db)
    children = [node for node in final["nodes"] if node["parent_id"] == connect["id"]]
    assert {node["title"] for node in children} == {"Connect Slack", "Connect Notes"}
    top_level = {node["title"] for node in final["nodes"] if node["parent_id"] is None}
    assert top_level == {step["title"] for step in applicable_steps(facts)}
    assert "Hand over to your agents" in top_level


def test_each_missing_paid_subscription_is_its_own_named_step():
    from onboarding.app_first_run import applicable_steps

    steps = applicable_steps({
        "sources_missing": {"claude_code": "needs_auth", "codex": "not_installed"},
        "pays_for": ["claude_code", "codex"],
    })
    assert [(step["title"], step["vendor"]) for step in steps if step["step_number"] == 1] == [
        ("Add your Claude subscription", "claude_code"),
        ("Add your ChatGPT subscription", "codex"),
    ]
