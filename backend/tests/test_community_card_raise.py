"""The server raises the community card when first-run-app step 7 becomes the next step."""

from __future__ import annotations

import json

import pytest

from infra import user_profile
from onboarding import community_card, setup_strings
from tests.test_app_first_run_seed import PASSWORD, _conductor_id, _rows, app_seed_facts, fresh_install  # noqa: F401

TITLE = setup_strings.COMMUNITY["title"]
STEP_7 = "How CORSIA gets better"


def _earlier() -> list[str]:
    """Titles of the app plan's top-level steps before step 7, in order."""
    rows = _rows(
        "SELECT title, workflow_step FROM plan_nodes WHERE workflow_ref='first-run-app' "
        "AND parent_id IS NULL AND archived_at IS NULL")
    numbered = sorted((int(row["workflow_step"].split(":")[0]), row["title"]) for row in rows)
    return [title for number, title in numbered if number < 7]


@pytest.fixture
def conductor(fresh_install, app_seed_facts):  # noqa: F811 - shared fixtures
    from infra import accounts

    accounts.create_first_account("newcomer", PASSWORD, first_run_path="app")
    return _conductor_id()


def _close(conductor_id: str, title: str) -> None:
    """Close the step with this title, after any open step beneath it."""
    from infra.database import get_db
    from plans import public

    db = get_db()
    try:
        actor = public.PlanActor(conductor_id, "newcomer", None)
        plan_id = public.ensure_current_plan(actor=actor, include_folded=True, db=db)["plan"]["id"]
        top = db.execute("SELECT id FROM plan_nodes WHERE plan_id=? AND title=?", (plan_id, title)).fetchone()
        below = db.execute(
            "SELECT id FROM plan_nodes WHERE parent_id=? AND state NOT IN "
            "('accepted','accepted_with_followup','failed','closed')", (top["id"],)).fetchall()
        for row in [*below, top]:
            node = db.execute("SELECT id, revision FROM plan_nodes WHERE id=?", (row["id"],)).fetchone()
            public.mutate(
                plan_id, "close_node",
                {"node_id": node["id"], "expected_revision": node["revision"], "reason": "settled for the test"},
                actor=actor, provenance="test", mutation_id=f"close:{node['id']}", db=db,
            )
    finally:
        db.close()


def _mark_done(conductor_id: str, title: str) -> None:
    """The agent marks the step with this title done."""
    from infra.database import get_db
    from plans import public

    db = get_db()
    try:
        actor = public.PlanActor(conductor_id, "newcomer", None)
        plan_id = public.ensure_current_plan(actor=actor, include_folded=True, db=db)["plan"]["id"]
        node = db.execute("SELECT id, revision FROM plan_nodes WHERE plan_id=? AND title=?", (plan_id, title)).fetchone()
        public.mutate(
            plan_id, "mark_done",
            {"node_id": node["id"], "expected_revision": node["revision"], "evidence": "done for the test"},
            actor=actor, provenance="test", mutation_id=f"done:{node['id']}", db=db,
        )
    finally:
        db.close()


def _cards() -> list[dict]:
    return _rows("SELECT id, metadata FROM artifact_identities WHERE type='plan-decision'")


def _intros(conductor_id: str) -> list[dict]:
    return _rows("SELECT content, metadata FROM messages WHERE id=?", f"community-card:{conductor_id}:intro")


async def _turn_end(thread_id: str) -> None:
    """End a Turn for the thread through the one writer every harness's terminal goes through."""
    from automation.plan_liveness_terminal import on_thread_turn_complete
    from automation.turn_end_effects import record_turn_end

    await record_turn_end(on_thread_turn_complete, thread_id)


def _raised() -> list[dict]:
    bodies = [json.loads(card["metadata"])["plan_decision"] for card in _cards()]
    return [body for body in bodies if body["state"] == "raised"]


async def test_card_is_raised_once_at_turn_end_after_the_last_earlier_step_settles(conductor):
    earlier = _earlier()
    for title in earlier[:-1]:
        _close(conductor, title)
    await _turn_end(conductor)
    assert _cards() == [] and _intros(conductor) == []

    _close(conductor, earlier[-1])
    assert _cards() == [] and _intros(conductor) == []  # nothing at close time, only at Turn end
    await _turn_end(conductor)

    [body] = _raised()
    assert (body["title"], body["state"]) == (TITLE, "raised")
    [step] = _rows("SELECT id FROM plan_nodes WHERE title=?", STEP_7)
    assert body["source_node_id"] == step["id"]
    [intro] = _intros(conductor)
    assert intro["content"] == setup_strings.COMMUNITY_INTRO
    assert json.loads(intro["metadata"])["turn_unbound_reason"] == "server_scripted"


async def test_skip_the_rest_in_the_agents_order_raises_nothing(conductor):
    earlier = _earlier()
    for title in earlier:
        _close(conductor, title)
    _close(conductor, STEP_7)
    _close(conductor, "Your files, and when agents ask first")
    await _turn_end(conductor)

    assert _cards() == [] and _intros(conductor) == []


async def test_a_step_7_the_agent_marked_done_in_the_same_turn_still_gets_its_card(conductor):
    for title in _earlier():
        _close(conductor, title)
    _mark_done(conductor, STEP_7)
    await _turn_end(conductor)

    assert len(_raised()) == 1 and len(_cards()) == 1 and len(_intros(conductor)) == 1
    await _turn_end(conductor)
    assert len(_cards()) == 1 and len(_intros(conductor)) == 1


async def test_a_second_turn_end_raises_nothing_more(conductor):
    for title in _earlier():
        _close(conductor, title)
    await _turn_end(conductor)
    await _turn_end(conductor)

    assert len(_raised()) == 1 and len(_cards()) == 1 and len(_intros(conductor)) == 1


async def test_a_failed_raise_is_finished_at_the_next_turn_end(conductor, monkeypatch):
    from plans import public

    for title in _earlier():
        _close(conductor, title)
    real = public.raise_decision
    monkeypatch.setattr(public, "raise_decision", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    await _turn_end(conductor)
    assert len(_cards()) == 1 and _raised() == []  # drafted and reviewed, not yet raised

    monkeypatch.setattr(public, "raise_decision", real)
    await _turn_end(conductor)

    assert len(_cards()) == 1 and len(_raised()) == 1 and len(_intros(conductor)) == 1


async def test_an_external_harness_turn_end_raises_the_card(conductor):
    from automation.turn_end_effects import record_external_turn_end

    for title in _earlier():
        _close(conductor, title)
    await record_external_turn_end(conductor)

    assert len(_raised()) == 1 and len(_intros(conductor)) == 1


async def test_a_card_failure_does_not_stop_a_turn_end_returning_its_effects(conductor, monkeypatch):
    from automation.turn_end_effects import TurnEndEffects, record_turn_end

    def boom(thread_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(community_card, "raise_when_next", boom)
    from automation.plan_liveness_terminal import on_thread_turn_complete

    effects = await record_turn_end(on_thread_turn_complete, conductor)

    assert isinstance(effects, TurnEndEffects)


async def test_another_threads_turn_end_raises_nothing(conductor):
    for title in _earlier():
        _close(conductor, title)

    assert community_card.raise_when_next("someone-else") is False
    await _turn_end("someone-else")

    assert _cards() == [] and _intros(conductor) == []


async def test_a_card_the_model_only_drafted_does_not_replace_the_one_the_server_raises(conductor):
    from infra.database import get_db
    from plans import public

    earlier = _earlier()
    for title in earlier:
        _close(conductor, title)
    db = get_db()
    try:
        actor = public.PlanActor(conductor, "newcomer", None)
        listing = public.ensure_current_plan(actor=actor, include_folded=True, db=db)
        step = next(item for item in listing["nodes"] if item["title"] == STEP_7)
        fields = {key: setup_strings.COMMUNITY[key] for key in ("title", "context", "tradeoff", "rationale")}
        fields |= {"recommendation": "A", "other_enabled": True, "options": [
            {"id": "A", "label": "x", "detail": "y"}, {"id": "B", "label": "z", "detail": "w"}]}
        public.deliver_deferred(public.draft_decision(
            listing["plan"]["id"], source_node_id=step["id"], mutation_id="model-draft",
            actor=actor, provenance="test", db=db, **fields))
    finally:
        db.close()

    await _turn_end(conductor)

    [body] = _raised()
    assert body["options"][0]["label"] == setup_strings.COMMUNITY["options"][0][0]


async def test_a_script_path_account_raises_nothing(fresh_install, app_seed_facts, monkeypatch):  # noqa: F811
    from infra import accounts
    from onboarding import first_run_wake

    monkeypatch.setattr(first_run_wake, "app_reference_served", lambda: False)
    accounts.create_first_account("newcomer", PASSWORD, first_run_path="app")
    conductor_id = _conductor_id()
    for title in ("Getting started", "Additional provider setup", "Configuration checks", "Connect web search"):
        _close(conductor_id, title)
    await _turn_end(conductor_id)

    assert _cards() == [] and _intros(conductor_id) == []


@pytest.mark.parametrize("line", [
    "- **Learning:** Yes",
    "- **Use community improvements** Yes",
    "- **Use improvements:** Yes",
    "- **Share improvements with the community:** Yes",
    "- **Use community improvements:** Maybe",
    "**Use community improvements:** Yes",
])
def test_near_miss_profile_lines_do_not_turn_the_library_on(tmp_path, line):
    (tmp_path / user_profile.PROFILE_RELATIVE_PATH).parent.mkdir(parents=True)
    (tmp_path / user_profile.PROFILE_RELATIVE_PATH).write_text(f"# User profile\n\n{line}\n", encoding="utf-8")

    assert user_profile.uses_community_improvements(tmp_path) is False
