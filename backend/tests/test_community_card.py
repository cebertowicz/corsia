"""CORSIA raises the community card itself when first-run step 7 becomes current.

The scene is the real app-path seed: a first account creates the CORSIA agent and
its ``first-run-app`` plan, and plan writes go through ``plans.boundary.mutate``.
"""

from __future__ import annotations

import json

import pytest

from infra import accounts, config, user_profile
import infra.community_card as community_card
from onboarding import setup_strings
from plans import public
from plans.boundary import PlanActor, mutate
from tests.test_app_first_run_seed import (  # noqa: F401 - shared fixtures
    PASSWORD, _conductor_id, _rows, app_seed_facts, fresh_install,
)

TITLE = setup_strings.COMMUNITY["title"]
LABELS = [label for label, _detail, _answer in setup_strings.COMMUNITY["options"]]


class Scene:
    """The seeded first-run plan of a fresh install."""

    def __init__(self) -> None:
        self.owner = _conductor_id()
        [thread] = _rows("SELECT user_id, project_id FROM threads WHERE id=?", self.owner)
        self.actor = PlanActor(self.owner, thread["user_id"], thread["project_id"])
        self.plan_id = _rows("SELECT id FROM plans WHERE owner_id=?", self.owner)[0]["id"]
        self.steps = {
            row["workflow_step"]: row["id"] for row in _rows(
                "SELECT id, workflow_step FROM plan_nodes WHERE plan_id=? AND workflow_ref='first-run-app'",
                self.plan_id,
            )
        }

    def update(self, operation: str, node_id: str, **params) -> dict:
        revision = _rows("SELECT revision FROM plan_nodes WHERE id=?", node_id)[0]["revision"]
        from infra.database import get_db

        db = get_db()
        try:
            return mutate(
                self.plan_id, operation, {"node_id": node_id, "expected_revision": revision, **params},
                actor=self.actor, provenance="test.community_card",
                mutation_id=f"{operation}-{node_id}-{revision}", db=db,
            )
        finally:
            db.close()

    def finish(self, step: str, **params) -> dict:
        return self.update("mark_done", self.steps[step], evidence="Done in the test", **params)

    def decisions(self) -> list[dict]:
        rows = _rows(
            "SELECT external_id, metadata FROM artifact_identities WHERE integration='local' AND type='plan-decision'",
        )
        return [{**json.loads(row["metadata"])["plan_decision"], "id": row["external_id"]} for row in rows]


@pytest.fixture
def scene(fresh_install, app_seed_facts):  # noqa: F811
    accounts.create_first_account("newcomer", PASSWORD, full_name="New Comer", first_run_path="app")
    return Scene()


def _only_card(scene: Scene) -> dict:
    [card] = scene.decisions()
    return card


def test_starting_step_7_raises_exactly_one_raised_community_card(scene, caplog):
    scene.update("activate", scene.steps["7:common"])

    card = _only_card(scene)
    assert (card["title"], card["state"], card["source_node_id"]) == (TITLE, "raised", scene.steps["7:common"])
    assert [option["label"] for option in card["options"]] == LABELS
    assert "task_title" not in card
    assert "not scheduled" not in caplog.text


def test_closing_step_6_with_step_7_next_raises_the_card(scene):
    scene.update("activate", scene.steps["6:common"])
    assert scene.decisions() == []

    scene.finish("6:common", next_node_id=scene.steps["7:common"])

    assert _only_card(scene)["source_node_id"] == scene.steps["7:common"]


def test_the_card_never_exists_before_step_7_is_current(scene):
    scene.update("activate", scene.steps["5:common"])
    scene.update("activate", scene.steps["6:common"])

    assert scene.decisions() == []


def test_another_steps_start_schedules_nothing(scene, monkeypatch):
    from plans import admission

    scheduled = []
    monkeypatch.setattr(admission, "_schedule_step_start", lambda *args: scheduled.append(args))
    for step in ("5:common", "8:common"):
        scene.update("activate", scene.steps[step])
        scene.finish(step)

    assert scheduled == []
    assert scene.decisions() == []


def test_plan_read_of_step_7_shows_whether_the_card_is_waiting(scene, monkeypatch):
    """Step 7's guidance tells a model to look for this: a raised card is a ``decision_raised`` wait."""
    from infra.database import get_db
    from plans import service, status

    def waiting() -> list[str]:
        db = get_db()
        try:
            plan = service._plan_row(db, scene.plan_id)
            node = status.node_detail(db, plan, scene.steps["7:common"], actor=scene.actor)
        finally:
            db.close()
        return [cause["reason_key"] for cause in node["execution"]["causes"]]

    real = community_card.raise_for_current_step
    monkeypatch.setattr(community_card, "raise_for_current_step", lambda *args, **kwargs: None)
    scene.update("activate", scene.steps["7:common"])
    assert "decision_raised" not in waiting()

    monkeypatch.setattr(community_card, "raise_for_current_step", real)
    db = get_db()
    try:
        community_card.raise_for_current_step(scene.steps["7:common"], scene.plan_id, db=db)
    finally:
        db.close()
    assert "decision_raised" in waiting()


def test_reselecting_step_7_after_a_release_raises_no_second_card(scene):
    from infra.database import write_transaction
    from plans import admission

    node_id = scene.steps["7:common"]
    scene.update("activate", node_id)
    assert _rows("SELECT 1 FROM plan_node_attempts WHERE node_id=? AND released_at IS NOT NULL", node_id)
    with write_transaction(None, scope="reselect_step_7") as db:
        admission.select(node_id, actor_id=scene.owner, provenance="test", db=db)

    assert len(scene.decisions()) == 1


def test_pausing_and_resuming_step_7_raises_no_second_card(scene):
    node_id = scene.steps["7:common"]
    scene.update("activate", node_id)
    human = PlanActor(None, scene.actor.user_id, None, kind="user")
    from infra.database import get_db

    def as_user(operation: str, **params) -> dict:
        revision = _rows("SELECT revision FROM plan_nodes WHERE id=?", node_id)[0]["revision"]
        db = get_db()
        try:
            return mutate(
                scene.plan_id, operation, {"node_id": node_id, "expected_revision": revision, **params},
                actor=human, provenance="test.community_card", mutation_id=f"{operation}-{revision}", db=db,
            )
        finally:
            db.close()

    as_user("pause_node", reason="Person paused the step")
    [hold] = _rows("SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", node_id)
    as_user("resume_node", hold_id=hold["id"])

    assert len(scene.decisions()) == 1


def test_no_card_when_the_plan_owner_is_not_the_corsia_agent(scene):
    from infra.database import write_transaction

    with write_transaction(None, scope="drop_corsia_agent") as db:
        db.execute("DELETE FROM settings WHERE key = 'corsia_agent_thread_id'")

    scene.update("activate", scene.steps["7:common"])

    assert scene.decisions() == []


def test_a_failing_raise_does_not_fail_the_start(scene, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("raise failed")

    monkeypatch.setattr(public, "draft_decision", broken)

    result = scene.update("activate", scene.steps["7:common"])

    assert result.result["node"]["state"] == "active"
    assert scene.decisions() == []


def test_answering_the_card_writes_the_profile_lines(scene, tmp_path, monkeypatch):
    from infra.database import get_db

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    scene.update("activate", scene.steps["7:common"])
    card = _only_card(scene)
    assert user_profile.uses_community_improvements(tmp_path) is False

    db = get_db()
    try:
        public.answer_decision(
            card["id"], option_id="A", expected_revision=card["revision"], mutation_id="answer-step-7",
            actor=PlanActor(None, scene.actor.user_id, None, kind="user"), provenance="test.community_card", db=db,
        )
    finally:
        db.close()

    assert user_profile.uses_community_improvements(tmp_path) is True


def test_a_failed_review_leaves_no_draft_and_a_retry_raises_one_card(scene, monkeypatch):
    real = public.parent_review_decision
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("review failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(public, "parent_review_decision", fail_once)

    scene.update("activate", scene.steps["7:common"])

    assert scene.decisions() == []

    from infra.database import get_db

    db = get_db()
    try:
        community_card.raise_for_current_step(scene.steps["7:common"], scene.plan_id, db=db)
    finally:
        db.close()

    assert _only_card(scene)["state"] == "raised"


def test_no_community_entry_raises_nothing(scene, monkeypatch):
    from onboarding import setup_decisions

    monkeypatch.setattr(setup_decisions, "community_card", lambda: (_ for _ in ()).throw(KeyError("x")))

    scene.update("activate", scene.steps["7:common"])

    assert scene.decisions() == []
