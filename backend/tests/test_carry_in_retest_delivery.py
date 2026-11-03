"""C1 re-test delivery: each side's input, the turn-bound check, own-file lessons, and same-input cases."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.test_carry_in_lifecycle import (  # noqa: F401 - c1 is the shared fixture
    ACTIONS, LESSON_ID, TARGET, _admitting_turn, _arm, _call, _events, _git, _local, _make_pending,
    _pending_fact, _save, _write_lesson, c1,
)

LESSON_BODY = "Never close a node before reading its rule (distinctive lesson body)."


def _read_turn_result(passing):
    """A ``read_turn_result`` whose Turn passes only on a side ``passing`` names (a label's first letter)."""
    async def read(thread_id, _turn_id):
        side = thread_id.rsplit("-", 1)[1][0]
        calls = [_call("read_file", path=TARGET), _call("plan_update", operation="mark_done")] if side in passing else []
        return SimpleNamespace(error=None, tool_calls=calls)

    return read


def _record_prompts(monkeypatch, passing):
    """Route samples through a fake Turn that admits the real prompt; returns each sample thread's prompt."""
    from eval import endpoint, turn_result

    prompts: dict[str, str] = {}
    admitting = _admitting_turn([])

    async def run_eval_turn(**kwargs):
        prompts[kwargs["thread_id"]] = kwargs["prompt"]
        return await admitting(**kwargs)

    monkeypatch.setattr(endpoint, "run_eval_turn", run_eval_turn)
    monkeypatch.setattr(turn_result, "read_turn_result", _read_turn_result(passing))
    return prompts


def _sides(prompts: dict[str, str], side: str) -> list[str]:
    return [text for name, text in prompts.items() if name.rsplit("-", 1)[1][0] == side]


@pytest.mark.asyncio
async def test_each_side_dispatches_once_its_recorded_input_matches(c1, monkeypatch):
    from core.lesson_delivery import NOTICE
    from eval import endpoint, turn_result
    from infra import carry_in_drain

    _write_lesson(c1, text=LESSON_BODY)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    fact = _pending_fact()
    _local(monkeypatch)
    dispatched: list = []
    monkeypatch.setattr(endpoint, "run_eval_turn", _admitting_turn(dispatched))
    monkeypatch.setattr(turn_result, "read_turn_result", _read_turn_result(""))
    owner = {"user_id": "steve", "project_id": None}
    baseline, candidate = _arm(c1, fact, deliver=False), _arm(c1, fact, deliver=True)

    await carry_in_drain._sample(fact, owner, "omlx@srv/m0", ACTIONS, baseline, "b1", [])
    await carry_in_drain._sample(fact, owner, "omlx@srv/m0", ACTIONS, candidate, "c1", [])

    prefix = f"eval-c1-{fact.identity[:16]}"
    assert dispatched == [f"turn-{prefix}-b1", f"turn-{prefix}-c1"]
    block = candidate.prompt.rsplit("\n\n", 1)[1]
    payload = json.loads(block)["trial_lessons"][0]["guidance"]
    # The delivery is the plan result's shape, and the two prompts differ only by it.
    assert json.loads(block) == {"trial_lessons": [{
        "lesson_id": LESSON_ID, "target": TARGET, "guidance": payload, "notice": NOTICE,
    }]}
    assert LESSON_BODY in payload and "trial_lessons" not in baseline.prompt
    assert candidate.prompt == baseline.prompt + "\n\n" + block


@pytest.mark.asyncio
@pytest.mark.parametrize(("arm_delivers", "input_delivers"), [(True, False), (False, True)])
async def test_a_turn_whose_input_does_not_match_its_side_is_refused_before_dispatch(
    c1, monkeypatch, arm_delivers, input_delivers,
):
    from eval import endpoint, turn_result
    from infra import carry_in_drain

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    fact = _pending_fact()
    _local(monkeypatch)
    dispatched: list = []
    admitting = _admitting_turn(dispatched)
    recorded = _arm(c1, fact, deliver=input_delivers)

    async def run_eval_turn(**kwargs):
        return await admitting(**{**kwargs, "prompt": recorded.prompt})

    monkeypatch.setattr(endpoint, "run_eval_turn", run_eval_turn)
    monkeypatch.setattr(turn_result, "read_turn_result", lambda *_a: pytest.fail("read a refused turn"))

    with pytest.raises(carry_in_drain._DeliveryMismatch):
        await carry_in_drain._sample(
            fact, {"user_id": "steve", "project_id": None}, "omlx@srv/m0", ACTIONS,
            _arm(c1, fact, deliver=arm_delivers), "c1", [],
        )
    assert dispatched == []


@pytest.mark.asyncio
async def test_a_baseline_whose_target_text_has_the_delivery_opener_passes_the_check(c1, monkeypatch):
    """The check is exact equality with the side's prompt: admission records it verbatim, odd whitespace included."""
    from infra import carry_in_drain

    text = 'a {\n  "trial_lessons": [ key\r\n\ttab and trailing spaces  \n\n'
    _write_lesson(c1, text=LESSON_BODY)
    _save(c1, text)
    await _make_pending(c1)
    fact = _pending_fact()
    _local(monkeypatch)
    prompts = _record_prompts(monkeypatch, "c")

    assert await carry_in_drain.wake(c1.workspace) is True

    assert (_events()[-1]["event"], _events()[-1]["verdict"]) == ("lesson.carry_in.result", "passed")
    assert all('{\n  "trial_lessons": [' in text for text in _sides(prompts, "b"))
    baseline = _arm(c1, fact, deliver=False, blob=text.encode())
    candidate = _arm(c1, fact, deliver=True, blob=text.encode())
    assert baseline.carried_by(baseline.prompt) and not baseline.carried_by(None)
    assert not baseline.carried_by(baseline.prompt + "\n") and not baseline.carried_by(candidate.prompt)
    assert not candidate.carried_by(baseline.prompt)


@pytest.mark.asyncio
async def test_a_trial_lessons_sides_see_the_same_target_revision(c1, monkeypatch):
    from infra import carry_in_drain

    _save(c1, "rule v0\n")
    await _make_pending(c1)
    _write_lesson(c1, text=LESSON_BODY)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    _local(monkeypatch)
    prompts = _record_prompts(monkeypatch, "c")

    assert await carry_in_drain.wake(c1.workspace) is True

    assert _events()[-1]["verdict"] == "passed"
    assert all("rule v1" in text and "rule v0" not in text for text in prompts.values())
    baseline, candidate = _sides(prompts, "b")[0], _sides(prompts, "c")[0]
    assert "trial_lessons" not in baseline and candidate.startswith(baseline + "\n\n") and "trial_lessons" in candidate


@pytest.mark.asyncio
async def test_a_non_trial_target_edit_compares_before_with_after_and_delivers_nothing(c1, monkeypatch):
    from infra import carry_in_drain

    _save(c1, "rule v0\n")
    await _make_pending(c1)
    _write_lesson(c1, state="watch", text=LESSON_BODY)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    _local(monkeypatch)
    prompts = _record_prompts(monkeypatch, "c")

    assert await carry_in_drain.wake(c1.workspace) is True

    assert _events()[-1]["verdict"] == "passed"
    assert all("trial_lessons" not in text for text in prompts.values())
    assert all("rule v0" in text and "rule v1" not in text for text in _sides(prompts, "b"))
    assert all("rule v1" in text and "rule v0" not in text for text in _sides(prompts, "c"))


@pytest.mark.asyncio
@pytest.mark.parametrize(("target_text", "retired"), [
    ("# Rules\n\n## Read first\nalready here\n", True),
    ("# Rules\n\n## Something else\n", False),
])
async def test_a_trial_candidate_follows_task_starts_omission_when_the_target_has_the_section(
    c1, monkeypatch, target_text, retired,
):
    from infra import carry_in_drain

    _write_lesson(c1, text=LESSON_BODY, section="Read first")
    _save(c1, target_text)
    await _make_pending(c1)
    _local(monkeypatch)
    prompts = _record_prompts(monkeypatch, "c")

    assert await carry_in_drain.wake(c1.workspace) is True

    terminal = _events()[-1]
    if retired:  # task start would deliver nothing, so both sides read the same prompt
        assert (terminal["event"], terminal["reason"]) == ("lesson.carry_in.retired", "same_input")
        assert prompts == {}
    else:
        assert terminal["verdict"] == "passed"
        assert all("Trial section for" in text for text in _sides(prompts, "c"))
        assert all("trial_lessons" not in text for text in _sides(prompts, "b"))


@pytest.mark.asyncio
async def test_a_retest_runs_baseline_without_and_candidate_with_the_delivery(c1, monkeypatch):
    from infra import carry_in_drain

    _write_lesson(c1, text=LESSON_BODY)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    _local(monkeypatch)
    prompts = _record_prompts(monkeypatch, "c")

    assert await carry_in_drain.wake(c1.workspace) is True

    terminal = _events()[-1]
    assert (terminal["event"], terminal["verdict"]) == ("lesson.carry_in.result", "passed")
    assert (terminal["baseline_failures"], terminal["candidate_passes"]) == (2, 2)
    assert all("trial_lessons" not in text for text in _sides(prompts, "b"))
    assert all('"trial_lessons"' in text and LESSON_ID in text and "Read first." not in text
               for text in _sides(prompts, "c"))


@pytest.mark.asyncio
async def test_a_baseline_that_passes_is_not_discriminating_and_the_candidate_never_runs(c1, monkeypatch):
    from infra import carry_in_drain

    _write_lesson(c1)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    _local(monkeypatch)
    prompts = _record_prompts(monkeypatch, "bc")

    assert await carry_in_drain.wake(c1.workspace) is True
    assert _events()[-1]["verdict"] == "not_discriminating"
    assert _sides(prompts, "c") == [] and len(_sides(prompts, "b")) == 2


@pytest.mark.parametrize(("state", "delivered"), [("trial", True), ("promoted", False), ("watch", False)])
@pytest.mark.asyncio
async def test_only_a_trial_lesson_is_delivered_to_the_candidate(c1, state, delivered):
    from core import carry_in_facts
    from infra.database import get_db

    _write_lesson(c1, state=state)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    case = carry_in_facts.eligible_case(get_db(), c1.workspace, _pending_fact())
    assert not isinstance(case, str)
    assert case.deliver is delivered


@pytest.mark.asyncio
async def test_a_lesson_without_a_target_is_its_own_target_and_its_save_starts_its_case(c1, monkeypatch):
    from core.carry_in_facts import record_committed_save
    from infra import carry_in_drain

    path = _write_lesson(c1, target=None, text=LESSON_BODY)
    lesson_rel = path.relative_to(c1.workspace).as_posix()
    commit = _git(["rev-parse", "HEAD"], cwd=c1.workspace).strip()
    record_committed_save(c1.workspace, [path], commit)
    await _make_pending(c1)
    fact = _pending_fact()
    assert (fact.target_path, fact.target_commit, fact.lesson_path) == (lesson_rel, commit, lesson_rel)

    _local(monkeypatch)
    prompts = _record_prompts(monkeypatch, "c")
    assert await carry_in_drain.wake(c1.workspace) is True

    assert _events()[-1]["verdict"] == "passed"
    assert all(LESSON_BODY not in text and "reads:" not in text and "no file" not in text for text in prompts.values())
    assert _sides(prompts, "b") == ["A node closed unread."] * 2
    assert all(lesson_rel in text for text in _sides(prompts, "c"))  # the index line names the lesson's SKILL.md


@pytest.mark.asyncio
async def test_saving_a_lesson_that_has_a_target_starts_its_case_on_the_target_at_that_commit(c1):
    from core.carry_in_facts import record_committed_save

    path = _write_lesson(c1)
    commit = _git(["rev-parse", "HEAD"], cwd=c1.workspace).strip()
    record_committed_save(c1.workspace, [path], commit)
    await _make_pending(c1)

    fact = _pending_fact()
    assert (fact.target_path, fact.target_commit) == (TARGET, commit)
    assert fact.lesson_path == path.relative_to(c1.workspace).as_posix()


@pytest.mark.parametrize("target", [None, TARGET])
@pytest.mark.asyncio
async def test_a_case_whose_sides_would_see_the_same_input_retires_before_any_sample(c1, monkeypatch, target):
    """A non-trial lesson gets no delivery: with no target, or on a lesson save that left
    its target unchanged, both sides would read the same prompt and could show no effect."""
    from core.carry_in_facts import record_committed_save
    from infra import carry_in_drain

    _save(c1, "rule v1\n")
    await _make_pending(c1)  # the target save, before any lesson names it, matches nothing
    path = _write_lesson(c1, state="watch", target=target)
    record_committed_save(c1.workspace, [path], _git(["rev-parse", "HEAD"], cwd=c1.workspace).strip())
    await _make_pending(c1)
    _pending_fact()
    _local(monkeypatch)
    prompts = _record_prompts(monkeypatch, "c")

    assert await carry_in_drain.wake(c1.workspace) is True
    terminal = _events()[-1]
    assert (terminal["event"], terminal["reason"]) == ("lesson.carry_in.retired", "same_input")
    assert prompts == {}


@pytest.mark.asyncio
async def test_an_index_line_trial_names_a_file_a_sample_thread_can_read_under_isolation(c1, monkeypatch):
    """The index line is all a target-less or skill-target trial delivers: its path must open in a sample."""
    from core.carry_in_facts import record_committed_save
    from eval.endpoint import open_eval_thread
    from eval.tool_allowlist import carry_in_tool_isolation
    from infra import state
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from tools import sandbox_config
    from tools.filesystem_rw import ReadFileTool
    from tools.registry import ToolRegistry

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [c1.workspace])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    path = _write_lesson(c1, target=None, text=LESSON_BODY)
    record_committed_save(c1.workspace, [path], _git(["rev-parse", "HEAD"], cwd=c1.workspace).strip())
    await _make_pending(c1)
    candidate = _arm(c1, _pending_fact(), deliver=True, blob=None)
    (entry,) = json.loads(candidate.prompt.rsplit("\n\n", 1)[1])["trial_lessons"]
    assert str(path.resolve()) in entry["guidance"] and entry["target"] == str(path.resolve())

    db = get_db()
    user = db.execute("SELECT user_id FROM threads WHERE id=?", (c1.owner,)).fetchone()[0]
    thread_id, _row = await open_eval_thread(
        user, "native/test", thread_id="eval-c1-readable-b1", parent_thread_id=c1.owner,
    )
    admission = admit_input(
        thread_id, role="user", content="sample", intent="apply_now", message_id="read-input",
        turn_id="read-turn", boot_epoch="test-boot", admitted_at="t", db=db,
    )
    assert admission.batch is not None
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=c1.workspace))
    thread_token = state._current_thread_id.set(thread_id)
    turn_token = state._current_turn_identity.set(admission.batch.identity)
    try:
        with carry_in_tool_isolation(candidate.unseen):
            result = await registry.execute("read_file", {"path": str(path.resolve())})
    finally:
        state._current_turn_identity.reset(turn_token)
        state._current_thread_id.reset(thread_token)
    assert LESSON_BODY in result and "record_only" not in result
    assert "right_call" not in result and "required_actions" not in result  # the delivered file, without its case


@pytest.mark.asyncio
async def test_a_trial_with_an_empty_target_section_retires_with_task_starts_reason_before_any_claim(c1, monkeypatch):
    from infra import carry_in_drain

    _write_lesson(c1, text=LESSON_BODY, section="")
    _save(c1, "# Rules\n")
    await _make_pending(c1)
    _local(monkeypatch)
    prompts = _record_prompts(monkeypatch, "c")

    assert await carry_in_drain.wake(c1.workspace) is True

    assert [e["event"] for e in _events()][-1] == "lesson.carry_in.retired"
    assert _events()[-1]["reason"] == "missing_section"
    assert _events("lesson.carry_in.claimed") == [] and prompts == {}


@pytest.mark.asyncio
async def test_a_step_bound_trials_candidate_carries_the_step_addition_not_the_index_line(c1, monkeypatch):
    from core import carry_in_facts
    from core.workflow_step_binding import StepBinding
    from infra import carry_in_drain

    _write_lesson(c1, text=LESSON_BODY, section="Draft")
    _save(c1, "# Ship\n\n## Draft\nWrite it.\n")
    await _make_pending(c1)
    asked = []

    def declared_binding(trial, skills):
        asked.append((trial.lesson.id, skills))
        return StepBinding("ship", frozenset({"draft"})), True

    monkeypatch.setattr(carry_in_facts, "declared_binding", declared_binding)
    _local(monkeypatch)
    prompts = _record_prompts(monkeypatch, "c")

    assert await carry_in_drain.wake(c1.workspace) is True

    assert asked and asked[0][0] == LESSON_ID
    assert _events()[-1]["verdict"] == "passed"  # a bound trial is never section-checked, as at task start
    candidates = _sides(prompts, "c")
    assert candidates and all("addition to this step's instruction" in text and LESSON_BODY in text for text in candidates)
    assert all("Trial section for" not in text for text in candidates)
    assert all("trial_lessons" not in text for text in _sides(prompts, "b"))


def _commit_lesson_file(c1, **kwargs):
    from core.carry_in_facts import record_committed_save

    path = _write_lesson(c1, **kwargs)
    record_committed_save(c1.workspace, [path], _git(["rev-parse", "HEAD"], cwd=c1.workspace).strip())


@pytest.mark.parametrize(("first", "changed", "new_case"), [
    ({}, {"review_after": "2099-02-02"}, False),  # metadata only: nothing either side receives changes
    ({}, {"text": LESSON_BODY + " Changed."}, True),  # a new behavioral hash
    ({"section": ""}, {"section": "Read first"}, True),  # a trial retired as missing_section, its section now set
    ({}, {"section": "Check first"}, True),  # the section it adds
    ({"state": "watch"}, {"state": "trial"}, True),  # candidate now receives the delivery
    ({}, {"target": "docs/OTHER.md"}, True),  # the target file
])
@pytest.mark.asyncio
async def test_a_later_commit_of_the_lesson_file_starts_a_case_whenever_either_side_would_receive_something_new(
    c1, first, changed, new_case,
):
    _commit_lesson_file(c1, **{"text": LESSON_BODY, **first})
    await _make_pending(c1)

    _commit_lesson_file(c1, **{"text": LESSON_BODY, **first, **changed})
    await _make_pending(c1)

    pending = _events("lesson.carry_in.pending")
    assert len(pending) == (2 if new_case else 1)
    assert len({event["delivery_sha256"] for event in pending}) == len(pending)
    assert len(_events("lesson.carry_in.saved_processed")) == 2


@pytest.mark.asyncio
async def test_a_step_binding_change_in_the_lesson_files_commit_starts_a_case(c1, monkeypatch):
    from core import carry_in_facts
    from core.workflow_step_binding import StepBinding

    bound = []
    monkeypatch.setattr(
        carry_in_facts, "declared_binding",
        lambda trial, skills: (StepBinding("ship", frozenset({"draft"})), True) if bound else (None, False),
    )
    _commit_lesson_file(c1, text=LESSON_BODY)
    await _make_pending(c1)
    bound.append(True)
    _commit_lesson_file(c1, text=LESSON_BODY, review_after="2099-02-02")
    await _make_pending(c1)

    assert len(_events("lesson.carry_in.pending")) == 2


@pytest.mark.asyncio
async def test_a_pending_fact_written_before_the_delivery_hash_never_matches(c1):
    from core import carry_in_facts
    from infra.database import get_db

    _commit_lesson_file(c1, text=LESSON_BODY)
    await _make_pending(c1)
    db = get_db()
    db.execute(
        "UPDATE outcome_events SET attributes=json_remove(attributes, '$.delivery_sha256') WHERE event_name=?",
        (carry_in_facts.PENDING,),
    )
    db.commit()
    _commit_lesson_file(c1, text=LESSON_BODY, review_after="2099-02-02")
    await _make_pending(c1)

    assert len(_events("lesson.carry_in.pending")) == 2
