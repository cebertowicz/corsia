from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ACTIONS = {
    "required_actions": [
        {"tool": "read_file", "params": {"path": "context/rule.md"}},
        {"tool": "plan_update", "params": {"operation": "mark_done"}},
    ],
    "forbidden_actions": [{"tool": "write_file", "params": {"path": "context/rule.md"}}],
    "decisive_action": {"tool": "plan_update", "params": {"operation": "mark_done"}},
}


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def _event_db() -> sqlite3.Connection:
    from infra.outcome_events_schema import OUTCOME_EVENTS_SCHEMA_SQL
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
        "VALUES ('owner', 'user', 'native/test', 0, 1, '2026-10-21T00:00:00+00:00', '2026-10-21T00:00:00+00:00')"
    )
    return db


def test_scores_required_decisive_action_after_its_precondition():
    from core.lesson_carry_in import score_trace

    score = score_trace(ACTIONS, [
        _call("read_file", {"path": "context/rule.md", "limit": 20}),
        _call("plan_update", {"operation": "mark_done", "node_id": "node-1"}),
    ])

    assert (score.verdict, score.reason, score.decisive_call_index) == ("pass", "required decisive action", 1)


def test_forbidden_action_wins_before_later_decisive_action():
    from core.lesson_carry_in import score_trace

    score = score_trace(ACTIONS, [
        _call("write_file", {"path": "context/rule.md", "content": "wrong"}),
        _call("read_file", {"path": "context/rule.md"}),
        _call("plan_update", {"operation": "mark_done"}),
    ])

    assert (score.verdict, score.reason, score.decisive_call_index) == ("fail", "forbidden decisive action", 0)


def test_decisive_action_without_its_precondition_fails():
    from core.lesson_carry_in import score_trace

    score = score_trace(ACTIONS, [_call("plan_update", {"operation": "mark_done"})])

    assert (score.verdict, score.reason) == ("fail", "required precondition missing before decisive action")


def test_no_decisive_action_fails():
    from core.lesson_carry_in import score_trace

    score = score_trace(ACTIONS, [_call("read_file", {"path": "context/rule.md"})])

    assert (score.verdict, score.reason, score.decisive_call_index) == ("fail", "no_decisive_action", None)


@dataclass
class Adapter:
    samples: list[list[dict[str, Any]]]
    is_ready: bool = True

    def ready(self) -> bool:
        return self.is_ready

    def run(self, **_kwargs) -> list[dict[str, Any]]:
        return self.samples.pop(0)


def test_local_adapter_reaches_only_the_shared_fresh_turn_runner():
    from core.lesson_carry_in import LocalTraceAdapter

    calls: list[dict[str, Any]] = []

    async def runner(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            result=SimpleNamespace(
                tool_calls=[_call("read_file", {"path": "context/rule.md"})],
                usage={"prompt_tokens": 12, "completion_tokens": 5},
            ),
            error=None,
        )

    adapter = LocalTraceAdapter("owner", runner=runner)

    assert adapter.run(target_path="context/rule.md", case={"right_call": "Read it."}) == [
        _call("read_file", {"path": "context/rule.md"})
    ]
    assert calls[0]["model"] == "omlx@ns-f378151a51/Qwen3.8-Flash-Next-MLX-oQ6-MTP"
    assert calls[0]["prompt"] == '{"case": {"right_call": "Read it."}, "target_path": "context/rule.md"}'
    assert adapter.last_receipt["usage"] == {"prompt_tokens": 12, "completion_tokens": 5}


def test_three_samples_settle_by_two_of_three():
    from core.lesson_carry_in import run_three

    passed = [_call("read_file", {"path": "context/rule.md"}), _call("plan_update", {"operation": "mark_done"})]
    failed = [_call("read_file", {"path": "context/rule.md"})]
    result = run_three(ACTIONS, Adapter([passed, failed, passed]))

    assert result.state == "settled"
    assert result.verdict == "pass"
    assert [score.verdict for score in result.scores] == ["pass", "fail", "pass"]


def test_promotion_requires_pre_lesson_failure_and_post_lesson_pass():
    from core.lesson_carry_in import is_discriminating, run_three

    passed = [_call("read_file", {"path": "context/rule.md"}), _call("plan_update", {"operation": "mark_done"})]
    failed = [_call("read_file", {"path": "context/rule.md"})]
    pre_lesson = run_three(ACTIONS, Adapter([failed, failed, passed]))
    post_lesson = run_three(ACTIONS, Adapter([passed, failed, passed]))

    assert pre_lesson.verdict == "fail"
    assert post_lesson.verdict == "pass"
    assert is_discriminating(pre_lesson, post_lesson) is True


def test_model_not_ready_stays_pending_without_a_model_call():
    from core.lesson_carry_in import run_three

    adapter = Adapter([], is_ready=False)
    result = run_three(ACTIONS, adapter)

    assert result.state == "pending"
    assert result.verdict is None
    assert adapter.samples == []


def test_committed_target_edit_finds_its_file_local_executable_case_without_a_workspace_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from core.lesson_carry_in import CarryInFact, _matching_facts
    from core.lesson_files import render_trial
    from core.lesson_trials import Trial

    workspace = tmp_path / "workspace"
    target = workspace / "context" / "rule.md"
    target.parent.mkdir(parents=True)
    target.write_text("old rule\n")
    source = workspace / ".threads" / "owner" / "skills" / "lesson" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(render_trial(
        lesson_id="12345678-1234-5678-1234-567812345678", owner="owner", name="lesson",
        description="A carry-in case.", text="Use the rule.",
        trial=Trial("hypothesis", "2030-01-01"), source_refs=[{"kind": "turn", "reference": "turn"}],
        target_file="context/rule.md", case={
            "what_happened": "The rule was missed.", "right_call": "Read it.",
            "does_not_apply": "For a draft.", "correction_subject": "message:1", "actions": ACTIONS,
        },
    ))

    unrelated = workspace / "node_modules" / "package" / "SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(source.read_text())
    monkeypatch.setattr(Path, "rglob", lambda *_args, **_kwargs: pytest.fail("recursive workspace walk"))

    facts = _matching_facts(_event_db(), workspace, [target], "a" * 40)

    assert facts == [
        CarryInFact(
            "12345678-1234-5678-1234-567812345678", "owner", "a" * 40,
            "context/rule.md", ".threads/owner/skills/lesson/SKILL.md#corsia.lesson.case",
        )
    ]


def test_matching_case_survives_a_symlinked_workspace_root(tmp_path: Path):
    from core.lesson_carry_in import CarryInFact, _matching_facts
    from core.lesson_files import render_trial
    from core.lesson_trials import Trial

    real = tmp_path / "real"
    real.mkdir()
    workspace = tmp_path / "workspace-link"
    workspace.symlink_to(real, target_is_directory=True)
    target = workspace / "context" / "rule.md"
    target.parent.mkdir(parents=True)
    target.write_text("rule\n")
    source = workspace / ".threads" / "owner" / "skills" / "lesson" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(render_trial(
        lesson_id="12345678-1234-5678-1234-567812345678", owner="owner", name="lesson",
        description="A carry-in case.", text="Use the rule.",
        trial=Trial("hypothesis", "2030-01-01"), source_refs=[{"kind": "turn", "reference": "turn"}],
        target_file="context/rule.md", case={
            "what_happened": "The rule was missed.", "right_call": "Read it.",
            "does_not_apply": "For a draft.", "correction_subject": "message:1", "actions": ACTIONS,
        },
    ))

    db = _event_db()
    facts = _matching_facts(db, workspace, [target], "a" * 40)

    assert facts == [
        CarryInFact(
            "12345678-1234-5678-1234-567812345678", "owner", "a" * 40,
            "context/rule.md", ".threads/owner/skills/lesson/SKILL.md#corsia.lesson.case",
        )
    ]
    from core.lesson_carry_in import drain_facts, is_pending
    runs = drain_facts(db, workspace, facts, Adapter([], is_ready=False))
    assert [run.state for run in runs] == ["pending"]
    assert is_pending(db, facts[0])


def test_external_carry_in_source_is_counted_without_retaining_its_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from core.lesson_carry_in import _matching_facts
    from core.lesson_files import render_trial
    from core.lesson_trials import Trial

    workspace = tmp_path / "workspace"
    target = workspace / "context" / "rule.md"
    target.parent.mkdir(parents=True)
    target.write_text("rule\n")
    source = tmp_path / "outside" / "SKILL.md"
    source.parent.mkdir()
    source.write_text(render_trial(
        lesson_id="12345678-1234-5678-1234-567812345678", owner="owner", name="lesson",
        description="A carry-in case.", text="Use the rule.",
        trial=Trial("hypothesis", "2030-01-01"), source_refs=[{"kind": "turn", "reference": "turn"}],
        target_file="context/rule.md", case={
            "what_happened": "The rule was missed.", "right_call": "Read it.",
            "does_not_apply": "For a draft.", "correction_subject": "message:1", "actions": ACTIONS,
        },
    ))
    monkeypatch.setattr("core.skill_data_conversion.installed_skill_files", lambda *_args: [source])
    db = _event_db()

    assert _matching_facts(db, workspace, [target], "a" * 40) == []
    row = db.execute(
        "SELECT subject_key,attributes FROM outcome_events WHERE event_name='learning_loop.carry_in_source_skipped'"
    ).fetchone()
    assert row["subject_key"] == "carry-in:12345678-1234-5678-1234-567812345678:" + "a" * 40
    assert json.loads(row["attributes"])["reason"] == "outside_workspace"
    assert str(source) not in row["attributes"]


def test_drain_settles_a_pending_case_with_a_mocked_local_adapter(tmp_path: Path):
    from core.lesson_carry_in import _matching_facts, drain_facts, is_pending, record_pending
    from core.lesson_files import render_trial
    from core.lesson_trials import Trial

    workspace = tmp_path / "workspace"
    target = workspace / "context" / "rule.md"
    target.parent.mkdir(parents=True)
    target.write_text("old rule\n")
    source = workspace / ".threads" / "owner" / "skills" / "lesson" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(render_trial(
        lesson_id="12345678-1234-5678-1234-567812345678", owner="owner", name="lesson",
        description="A carry-in case.", text="Use the rule.",
        trial=Trial("hypothesis", "2030-01-01"), source_refs=[{"kind": "turn", "reference": "turn"}],
        target_file="context/rule.md", case={
            "what_happened": "The rule was missed.", "right_call": "Read it.",
            "does_not_apply": "For a draft.", "correction_subject": "message:1", "actions": ACTIONS,
        },
    ))
    fact = _matching_facts(_event_db(), workspace, [target], "a" * 40)[0]
    db = _event_db()
    passed = [_call("read_file", {"path": "context/rule.md"}), _call("plan_update", {"operation": "mark_done"})]

    assert record_pending(db, fact)
    runs = drain_facts(db, workspace, [fact], Adapter([passed, passed, passed]))

    assert [run.verdict for run in runs] == ["pass"]
    assert not is_pending(db, fact)


def test_committed_save_drain_uses_the_admitted_adapter(tmp_path: Path):
    from core.lesson_carry_in import _drain_committed_facts
    from core.lesson_files import render_trial
    from core.lesson_trials import Trial

    workspace = tmp_path / "workspace"
    target = workspace / "context" / "rule.md"
    target.parent.mkdir(parents=True)
    target.write_text("rule\n")
    source = workspace / ".threads" / "owner" / "skills" / "lesson" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(render_trial(
        lesson_id="12345678-1234-5678-1234-567812345678", owner="owner", name="lesson",
        description="A carry-in case.", text="Use it.",
        trial=Trial("hypothesis", "2030-01-01"), source_refs=[{"kind": "turn", "reference": "turn"}],
        target_file="context/rule.md", case={"what_happened": "Missed.", "right_call": "Read.",
        "does_not_apply": "Draft.", "correction_subject": "message:1", "actions": ACTIONS},
    ))
    db = _event_db()
    passed = [_call("read_file", {"path": "context/rule.md"}), _call("plan_update", {"operation": "mark_done"})]
    users: list[str] = []

    def adapter_factory(user_id: str) -> Adapter:
        users.append(user_id)
        return Adapter([passed, passed, passed])

    _drain_committed_facts(db, workspace, [target], "a" * 40, adapter_factory=adapter_factory)

    assert users == ["user"]
    assert db.execute("SELECT event_name FROM outcome_events WHERE event_name='learning_loop.carry_in_result'").fetchone()


def test_failed_thread_target_maps_once_to_a_self_caught_carry_in_regression(tmp_path: Path):
    from core.lesson_carry_in import CarryInFact, drain_facts, record_pending

    workspace = tmp_path / "workspace"
    target = workspace / ".threads" / "owner" / "context" / "rule.md"
    target.parent.mkdir(parents=True)
    target.write_text("old rule\n")
    lesson_id = "12345678-1234-5678-1234-567812345678"
    fact = CarryInFact(lesson_id, "owner", "a" * 40, ".threads/owner/context/rule.md", ".threads/owner/skills/lesson/SKILL.md#corsia.lesson.case")
    case_source = workspace / ".threads" / "owner" / "skills" / "lesson" / "SKILL.md"
    case_source.parent.mkdir(parents=True)
    from core.lesson_files import render_trial
    from core.lesson_trials import Trial
    case_source.write_text(render_trial(
        lesson_id=lesson_id, owner="owner", name="lesson", description="A carry-in case.", text="Use it.",
        trial=Trial("hypothesis", "2030-01-01"), source_refs=[{"kind": "turn", "reference": "turn"}],
        target_file=fact.target_path, case={"what_happened": "Missed.", "right_call": "Read.",
        "does_not_apply": "Draft.", "correction_subject": "message:1", "actions": ACTIONS},
    ))
    db = _event_db()
    failed = [_call("read_file", {"path": "context/rule.md"})]

    assert record_pending(db, fact)
    assert [run.verdict for run in drain_facts(db, workspace, [fact], Adapter([failed, failed, failed]))] == ["fail"]
    assert drain_facts(db, workspace, [fact], Adapter([failed, failed, failed])) == []
    rows = db.execute("SELECT attributes FROM outcome_events WHERE event_name='measurement.correction'").fetchall()
    assert len(rows) == 1
    assert 'carry_in_regression:' in rows[0]["attributes"]


def test_pending_result_and_retirement_are_queryable_outcome_events():
    from core.lesson_carry_in import CarryInFact, CarryInRun, TraceScore, is_pending, record_pending, record_result, record_retirement

    db = _event_db()
    fact = CarryInFact("lesson-1", "owner", "a" * 40, "context/rule.md", ".threads/owner/skills/lesson/SKILL.md#corsia.lesson.case")
    assert record_pending(db, fact)
    assert is_pending(db, fact)

    run = CarryInRun("settled", (TraceScore("pass", "required decisive action", 0),) * 3)
    assert record_result(db, fact, run)
    assert not is_pending(db, fact)

    retired = CarryInFact("lesson-2", "owner", "b" * 40, "context/rule.md", ".threads/owner/skills/lesson/SKILL.md#corsia.lesson.case")
    assert record_pending(db, retired)
    assert record_retirement(db, retired, "superseded by a corrected rule")
    assert not is_pending(db, retired)
    row = db.execute("SELECT attributes FROM outcome_events WHERE event_name='learning_loop.carry_in_retirement'").fetchone()
    assert 'superseded by a corrected rule' in row["attributes"]


def test_legacy_case_without_actions_remains_valid():
    from core.lesson_trials import lesson_errors

    case = {
        "what_happened": "A result lacked its commit.",
        "right_call": "Name the commit.",
        "does_not_apply": "For a draft.",
        "correction_subject": "message:41",
    }
    block = {
        "id": "12345678-1234-5678-1234-567812345678", "state": "trial", "owner": "owner",
        "review_after": "2030-01-01", "source_refs": [{"kind": "turn", "reference": "turn"}], "case": case,
    }

    assert lesson_errors(block, "a lesson", "short body") == []


def test_case_actions_are_preserved_as_a_file_local_contract():
    from core.lesson_trials import lesson_errors, parse_lesson

    case = {
        "what_happened": "A result lacked its commit.",
        "right_call": "Name the commit.",
        "does_not_apply": "For a draft.",
        "correction_subject": "message:41",
        "actions": ACTIONS,
    }
    block = {
        "id": "12345678-1234-5678-1234-567812345678", "state": "trial", "owner": "owner",
        "review_after": "2030-01-01", "source_refs": [{"kind": "turn", "reference": "turn"}], "case": case,
    }

    assert lesson_errors(block, "a lesson", "short body") == []
    assert parse_lesson({"corsia": {"lesson": block}}).case["actions"] == ACTIONS


def test_refuses_a_decisive_action_that_is_not_required():
    from core.lesson_carry_in import validate_actions

    invalid = {**ACTIONS, "decisive_action": {"tool": "write_file", "params": {"path": "x"}}}
    with pytest.raises(ValueError, match="must be one declared required action"):
        validate_actions(invalid)
