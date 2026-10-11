"""RT-5: a next step goes on the plan, so continuity_edit takes no new next_actions.

Existing entries drain through the rotating review; the checkpoint narrative
carries the step in progress, the last command and its result, and the next action.
"""

from __future__ import annotations

import pytest

from prompts.checkpoint import build_checkpoint_system_prompt
from tests.test_checkpoint_placement import _lesson
from tests.test_continuity_staging import _db, _running, _seed_thread, _tool_db


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "next.db"
    db = _db(str(path))
    _seed_thread(db)
    _running(db)
    db.commit()
    db.close()
    return _tool_db(path, monkeypatch)


@pytest.mark.parametrize("checkpoint_id", [None, "cp-1"])
def test_adding_a_next_action_is_refused_with_a_pointer_to_the_plan(setup, checkpoint_id) -> None:
    result = setup._run(
        "thread-1", "add", {"field": "next_actions", "body": _lesson("run the suite")}, checkpoint_id,
    )

    assert result.startswith("Error: next_actions takes no new entries")
    assert "plan_update" in result


def test_other_fields_still_take_an_add(setup) -> None:
    result = setup._run("thread-1", "add", {"field": "facts", "body": _lesson("a plain fact")})

    assert result.startswith("Recorded as entry")


def test_the_checkpoint_prompt_names_the_step_the_command_and_the_next_action() -> None:
    prompt = " ".join(build_checkpoint_system_prompt().split())

    assert "the step in progress, the last command run and its result, and the exact next action" in prompt
    assert "continuity_edit refuses a new next_actions entry" in prompt


def test_the_review_text_says_how_a_next_step_drains() -> None:
    from core.placement_review import _THE_REST

    text = " ".join(_THE_REST.split())

    assert "next_actions: a step still to do stays until it is done: reaffirm it" in text
    assert "Drop only a step that is done or stale" in text
    assert "Record no new next step here" in text
