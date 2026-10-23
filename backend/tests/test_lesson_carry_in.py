from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import sqlite3
import threading
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


def _event_db(path: str = ":memory:") -> sqlite3.Connection:
    from infra.outcome_events_schema import OUTCOME_EVENTS_SCHEMA_SQL
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
        "VALUES ('owner', 'user', 'native/test', 0, 1, '2026-10-21T00:00:00+00:00', '2026-10-21T00:00:00+00:00')"
    )
    db.commit()
    return db


@pytest.mark.asyncio
async def test_carry_in_eval_creates_its_own_unattributed_thread(monkeypatch: pytest.MonkeyPatch):
    from eval.fresh_turn import FreshTurnScope, _open_eval_thread

    calls = []

    async def run_in_db(function, *args, **kwargs):
        calls.append((function, args, kwargs))
        return {"id": "eval-1", "user_id": "user"}

    monkeypatch.setattr("infra.db_async.run_in_db", run_in_db)
    thread_id, row = await _open_eval_thread(
        "user", "omlx@server/model",
        scope=FreshTurnScope(project_id="project", owner_thread_id="owner"),
    )

    creation = calls[0][0]
    assert thread_id.startswith("eval-")
    assert thread_id != "owner"
    assert creation.keywords["thread_id"] == thread_id
    assert row["id"] == "eval-1"
    assert creation.keywords["project_id"] == "project"
    assert creation.keywords["parent_thread_id"] is None
    assert creation.keywords["spawned_by"] is None
    assert "actor_thread_id" not in creation.keywords


def test_carry_in_scope_marks_only_carry_in_turns_for_record_only_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from core.lesson_carry_in import _fresh_scope

    db = _event_db()
    monkeypatch.setattr("core.lesson_carry_in.target_scope_owner", lambda *_args: "owner")

    scope = _fresh_scope(db, tmp_path, "context/rule.md")

    assert scope.owner_thread_id == "owner"
    assert scope.carry_in is True


def _fenced_context_db(workspace: Path):
    from infra.database import get_db

    db = get_db()
    owner_file = workspace / ".threads" / "owner" / "context" / "OWNER.md"
    owner_file.parent.mkdir(parents=True)
    owner_file.write_text("owner context\n")
    db.execute(
        "UPDATE threads SET bootloader_files=? WHERE id='owner'",
        (json.dumps([str(owner_file)]),),
    )
    db.execute(
        "INSERT INTO threads (id,user_id,model,history_epoch,next_message_seq,created_at,updated_at) "
        "VALUES ('eval-1', 'user', 'native/test', 0, 1, 't', 't')"
    )
    db.commit()
    return db


class _NoCloseDb:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


def test_fenced_context_attach_journals_and_detach_removes_fixture_and_thread(
    isolated_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from eval.fresh_turn_context import FencedTarget, attach_fenced_context, detach_and_delete
    from infra import config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = _fenced_context_db(workspace)
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr("eval.fresh_turn_context._normally_delivered_target", lambda *_args: False)
    receipt = attach_fenced_context(
        "eval-1", "user", owner_thread_id="owner", project_id=None,
        target=FencedTarget(".threads/owner/context/OWNER.md", "a" * 40, b"fenced target\n", "edit"),
    )

    assert receipt.fixture is not None and receipt.fixture.read_bytes() == b"fenced target\n"
    assert db.execute("SELECT action FROM context_bootloader_events").fetchone()["action"] == "attach_eval_fenced_context"
    assert str(receipt.fixture) in json.loads(db.execute("SELECT bootloader_files FROM threads WHERE id='eval-1'").fetchone()[0])

    asyncio.run(detach_and_delete(receipt))

    assert not receipt.fixture.exists()
    assert db.execute("SELECT id FROM threads WHERE id='eval-1'").fetchone() is None
    assert [row["action"] for row in db.execute("SELECT action FROM context_bootloader_events ORDER BY created_at")] == [
        "attach_eval_fenced_context", "detach_eval_fenced_context",
    ]


def test_fenced_context_failed_attach_unlinks_its_fixture(
    isolated_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from eval.fresh_turn_context import FencedTarget, attach_fenced_context
    from infra import config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = _fenced_context_db(workspace)
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr("eval.fresh_turn_context._normally_delivered_target", lambda *_args: False)

    def refuse(*_args, **_kwargs):
        raise ValueError("attach refused")

    monkeypatch.setattr("api.context_bootloader._write_context_change", refuse)
    with pytest.raises(ValueError, match="attach refused"):
        attach_fenced_context(
            "eval-1", "user", owner_thread_id="owner", project_id=None,
            target=FencedTarget(".threads/owner/context/OWNER.md", "a" * 40, b"fenced target\n", "edit"),
        )
    assert not (workspace / ".threads" / "eval-1" / "context" / "CARRY_IN_TARGET.md").exists()


def test_normally_delivered_fenced_context_skips_fixture_and_still_deletes_thread(
    isolated_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from eval.fresh_turn_context import FencedTarget, attach_fenced_context, detach_and_delete
    from infra import config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = _fenced_context_db(workspace)
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr("eval.fresh_turn_context._normally_delivered_target", lambda *_args: True)
    receipt = attach_fenced_context(
        "eval-1", "user", owner_thread_id="owner", project_id=None,
        target=FencedTarget(".threads/owner/context/OWNER.md", "a" * 40, b"fenced target\n", "edit"),
    )

    assert receipt.fixture is None
    assert db.execute("SELECT action FROM context_bootloader_events").fetchall() == []
    asyncio.run(detach_and_delete(receipt))
    assert db.execute("SELECT id FROM threads WHERE id='eval-1'").fetchone() is None


def test_project_context_target_uses_normal_fresh_turn_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from eval.fresh_turn_context import _normally_delivered_target

    workspace = tmp_path / "workspace"
    target = workspace / "projects" / "project" / "context" / "PROJECT.md"
    target.parent.mkdir(parents=True)
    target.write_text("project context\n")
    calls = []

    def static(thread_id, project_id, root, db):
        calls.append((thread_id, project_id, root, db))
        return [SimpleNamespace(inclusion="included", metadata={"path": str(target)})], None, None

    monkeypatch.setattr("core.dynamic_context_registry._static_sources", static)
    db = object()

    assert _normally_delivered_target(db, workspace, "eval-1", "project", target.resolve())
    assert calls == [("eval-1", "project", workspace, db)]


def test_eval_threads_are_excluded_from_owner_measurement_and_lesson_scopes():
    from core.measurement_scope import scope_cte

    db = _event_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,project_id,parent_thread_id,created_at,updated_at) "
        "VALUES ('eval-sample', 'user', 'native/test', 'project', NULL, 't', 't')"
    )
    db.execute("UPDATE threads SET project_id='project', agent_enabled=1, agent_status='active' WHERE id='owner'")
    db.commit()
    params = {"user_id": "user", "scope_project": "project", "scope_agent": "owner"}

    rows = db.execute(scope_cte() + "SELECT id FROM scope_threads ORDER BY id", params).fetchall()

    assert [row["id"] for row in rows] == ["owner"]


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


def test_local_adapter_sends_only_the_scenario_to_the_shared_fresh_turn_runner(monkeypatch: pytest.MonkeyPatch):
    from core.lesson_carry_in import LocalTraceAdapter, scenario_for_case
    from llm.resolution import ExecutionTarget

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

    target = ExecutionTarget("local-id", "native", "omlx", "network", "omlx@server/model", None, 1, True, None, "Local", True)
    monkeypatch.setattr("core.lesson_carry_in_contract.ready_local_target", lambda: target)
    adapter = LocalTraceAdapter("owner", runner=runner)
    adapter._target = target
    scenario = scenario_for_case({
        "what_happened": "The context was missed.", "right_call": "Read it.",
        "does_not_apply": "Drafts.", "actions": ACTIONS,
    })

    assert adapter.run(scenario=scenario) == [_call("read_file", {"path": "context/rule.md"})]
    assert calls[0]["model"] == "omlx@server/model"
    assert calls[0]["prompt"] == '{"situation": "The context was missed."}'
    assert "right_call" not in calls[0]["prompt"]
    assert "does_not_apply" not in calls[0]["prompt"]
    assert "required_actions" not in calls[0]["prompt"]
    assert "context/rule.md" not in calls[0]["prompt"]
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
    from core.lesson_carry_in_contract import is_discriminating, run_three

    passed = [_call("read_file", {"path": "context/rule.md"}), _call("plan_update", {"operation": "mark_done"})]
    failed = [_call("read_file", {"path": "context/rule.md"})]
    pre_lesson = run_three(ACTIONS, Adapter([failed, failed, passed]))
    post_lesson = run_three(ACTIONS, Adapter([passed, failed, passed]))

    assert pre_lesson.verdict == "fail"
    assert post_lesson.verdict == "pass"
    assert is_discriminating(pre_lesson, post_lesson) is True


def test_eval_context_setup_error_is_pending_not_scored_as_a_failed_decisive_call(monkeypatch: pytest.MonkeyPatch):
    from core.lesson_carry_in import LocalTraceAdapter, run_three
    from llm.resolution import ExecutionTarget

    async def runner(**_kwargs):
        return SimpleNamespace(result=None, error="Eval context setup errored: carry-in target is not exactly one owner thread bootloader entry")

    target = ExecutionTarget("local-id", "native", "omlx", "network", "omlx@server/model", None, 1, True, None, "Local", True)
    monkeypatch.setattr("core.lesson_carry_in_contract.ready_local_target", lambda: target)
    adapter = LocalTraceAdapter("owner", runner=runner)
    adapter._target = target

    result = run_three(ACTIONS, adapter, before_sample=lambda: ("target-hash", None))

    assert result.state == "pending"
    assert result.reason == "Eval context setup errored: carry-in target is not exactly one owner thread bootloader entry"


def test_fixture_hash_mismatch_refuses_scoring_before_a_decisive_call():
    from core.lesson_carry_in import run_three

    class MismatchedFixtureAdapter(Adapter):
        def run(self, **_kwargs) -> list[dict[str, Any]]:
            self.last_receipt = {"fixture_sha": "not-the-recorded-blob"}
            return super().run()

    passed = [_call("read_file", {"path": "context/rule.md"}), _call("plan_update", {"operation": "mark_done"})]
    result = run_three(ACTIONS, MismatchedFixtureAdapter([passed]), before_sample=lambda: ("recorded-blob", None))

    assert (result.state, result.reason, result.scores) == ("pending", "fixture_blob_mismatch", ())


def test_newest_result_is_used_as_the_model_baseline():
    from core.lesson_carry_in import CarryInFact, CarryInRun, TraceScore, record_result
    from core.lesson_carry_in_result_queries import baseline_result

    db = _event_db()
    score = TraceScore("pass", "required decisive action", 1)
    first = CarryInFact("lesson", "owner", "a" * 40, "context/rule.md", "case", "edit")
    newest = CarryInFact("lesson", "owner", "b" * 40, "context/rule.md", "case", "rebaseline")
    run = CarryInRun("settled", (score,) * 3, "omlx@server/model", "resolved-model", "blob-sha")
    assert record_result(db, first, run)
    assert record_result(db, newest, run)

    assert baseline_result(db, first) == (
        newest,
        '{"resolved_identity": "resolved-model", "selector": "omlx@server/model"}',
    )


def test_baseline_result_streams_only_matching_rows():
    from core.lesson_carry_in import CarryInFact
    from core.lesson_carry_in_result_queries import baseline_result

    fact = CarryInFact("lesson", "owner", "a" * 40, "context/rule.md", "case")

    class Rows:
        def __iter__(self):
            return iter([{
                "attributes": json.dumps({
                    "lesson_id": "lesson", "target_path": "context/rule.md", "commit": "b" * 40,
                    "role": "edit", "model_identity": "model",
                }),
            }])

        def fetchall(self):
            pytest.fail("baseline_result must not materialize matching rows")

    class Db:
        def execute(self, query, params):
            assert "json_extract(attributes, '$.lesson_id')=?" in query
            assert "json_extract(attributes, '$.target_path')=?" in query
            assert params[1:] == ("lesson", "context/rule.md")
            return Rows()

    assert baseline_result(Db(), fact) == (
        CarryInFact("lesson", "owner", "b" * 40, "context/rule.md", "case"), "model",
    )


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

    facts, skipped = _matching_facts(_event_db(), workspace, [target], "a" * 40)

    assert skipped == []
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
    facts, _skipped = _matching_facts(db, workspace, [target], "a" * 40)

    assert facts == [
        CarryInFact(
            "12345678-1234-5678-1234-567812345678", "owner", "a" * 40,
            "context/rule.md", ".threads/owner/skills/lesson/SKILL.md#corsia.lesson.case",
        )
    ]
    from core.lesson_carry_in import drain_facts, is_pending, record_pending
    record_pending(db, facts[0])
    db.commit()
    runs = drain_facts(db, workspace, facts, Adapter([], is_ready=False))
    assert [run.state for run in runs] == ["pending"]
    assert is_pending(db, facts[0])


def test_thread_skill_target_is_skipped_without_an_unlineaged_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from core.lesson_carry_in import _matching_facts
    from core.lesson_files import render_trial
    from core.lesson_trials import Trial

    workspace = tmp_path / "workspace"
    target = workspace / ".threads" / "owner" / "skills" / "lesson" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("old rule\n")
    source = workspace / ".threads" / "owner" / "skills" / "lesson" / "SKILL.md"
    source.write_text(render_trial(
        lesson_id="12345678-1234-5678-1234-567812345678", owner="owner", name="lesson",
        description="A carry-in case.", text="Use the rule.",
        trial=Trial("hypothesis", "2030-01-01"), source_refs=[{"kind": "turn", "reference": "turn"}],
        target_file=".threads/owner/skills/lesson/SKILL.md", case={
            "what_happened": "The rule was missed.", "right_call": "Read it.",
            "does_not_apply": "For a draft.", "correction_subject": "message:1", "actions": ACTIONS,
        },
    ))
    db = _event_db()

    facts, skipped = _matching_facts(db, workspace, [target], "a" * 40)

    assert facts == []
    assert [reason for _source, reason in skipped] == ["thread_skill_no_unlineaged_route"]
    assert db.execute("SELECT COUNT(*) FROM outcome_events").fetchone()[0] == 0


def test_coverage_counts_skipped_thread_skill_targets_in_the_denominator():
    from core.lesson_carry_in import CarryInFact, CarryInRun, TraceScore, record_result, record_source_skipped
    from core.lesson_carry_in_coverage import coverage_counts

    db = _event_db()
    fact = CarryInFact("lesson", "owner", "a" * 40, "context/rule.md", "case")
    run = CarryInRun("settled", (TraceScore("pass", "ok", 0),) * 3)
    assert record_result(db, fact, run)
    assert record_source_skipped(
        db, CarryInFact("skill", "owner", "b" * 40, ".threads/owner/skills/lesson/SKILL.md", "skipped-source"),
        "thread_skill_no_unlineaged_route",
    )

    assert coverage_counts(db) == {"covered": 1, "skipped": 1, "total": 2}


def test_external_carry_in_source_is_counted_without_retaining_its_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from core.lesson_carry_in import _matching_facts, record_source_skipped
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

    facts, skipped = _matching_facts(db, workspace, [target], "a" * 40)
    assert facts == []
    for skipped_source, reason in skipped:
        record_source_skipped(db, skipped_source, reason)
    row = db.execute(
        "SELECT subject_key,attributes FROM outcome_events WHERE event_name='learning_loop.carry_in_source_skipped'"
    ).fetchone()
    assert row["subject_key"] == "carry-in:12345678-1234-5678-1234-567812345678:" + "a" * 40
    assert json.loads(row["attributes"])["reason"] == "outside_workspace"
    assert str(source) not in row["attributes"]


def test_drain_settles_a_pending_case_with_a_mocked_local_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    fact = _matching_facts(_event_db(), workspace, [target], "a" * 40)[0][0]
    db = _event_db()
    passed = [_call("read_file", {"path": "context/rule.md"}), _call("plan_update", {"operation": "mark_done"})]

    monkeypatch.setattr("core.lesson_carry_in._target_snapshot", lambda *_args: ("target-hash", None))
    assert record_pending(db, fact)
    db.commit()
    runs = drain_facts(db, workspace, [fact], Adapter([passed, passed, passed]))

    assert [run.verdict for run in runs] == ["pass"]
    assert not is_pending(db, fact)


def test_ready_free_save_starts_the_admitted_drain_without_another_trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from core.lesson_carry_in_drain import record_committed_save
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

    class ReadyAdapter(Adapter):
        def __init__(self, user_id: str, *_args):
            users.append(user_id)
            super().__init__([passed, passed, passed])

    class ImmediateThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

        def join(self):
            return None

    class NoCloseDb:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    monkeypatch.setattr("core.lesson_carry_in._target_snapshot", lambda *_args: ("target-hash", None))
    monkeypatch.setattr("core.lesson_carry_in_drain.LocalTraceAdapter", ReadyAdapter)
    monkeypatch.setattr("core.lesson_carry_in_drain._fresh_scope", lambda *_args: None)
    monkeypatch.setattr("core.lesson_carry_in_drain.threading.Thread", ImmediateThread)
    monkeypatch.setattr("infra.database.get_db", lambda: NoCloseDb())
    record_committed_save(workspace, [target], "a" * 40)

    assert users == ["user"]
    assert db.execute("SELECT event_name FROM outcome_events WHERE event_name='learning_loop.carry_in_result'").fetchone()


def test_pool_reset_waits_for_running_drain_threads(isolated_db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Pool teardown must wait for running drains before it can close their connections."""
    from tests.conftest import _reset_db_pool
    from core.lesson_carry_in_drain import schedule_pending_drain
    from infra.database import get_db

    entered = threading.Barrier(3)
    release = threading.Event()
    pool_closed = threading.Event()
    connection_ids: set[int] = set()

    def blocked_drain(_workspace: Path) -> None:
        connection_ids.add(id(getattr(get_db(), "_conn")))
        entered.wait(timeout=5)
        assert release.wait(timeout=5)

    class Pool:
        def shutdown_pool(self, _reconfigure=None) -> None:
            pool_closed.set()

    monkeypatch.setattr("core.lesson_carry_in_drain._drain_pending", blocked_drain)
    schedule_pending_drain(tmp_path)
    schedule_pending_drain(tmp_path)
    entered.wait(timeout=5)

    reset = threading.Thread(target=lambda: _reset_db_pool(Pool()))
    reset.start()
    assert not pool_closed.wait(timeout=0.05)
    release.set()
    reset.join(timeout=5)

    assert pool_closed.is_set()
    assert len(connection_ids) == 2


def test_shutdown_keeps_drain_scheduling_closed_until_process_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A catalog notification after drain shutdown must not reopen the database race."""
    from core.lesson_carry_in_drain import schedule_pending_drain, shutdown_drain_threads

    started: list[object] = []

    class Thread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            started.append(self)

        def join(self):
            return None

    monkeypatch.setattr("core.lesson_carry_in_drain.threading.Thread", Thread)
    monkeypatch.setattr("core.lesson_carry_in_drain._DRAINS_STOPPING", False)
    shutdown_drain_threads()
    schedule_pending_drain(tmp_path)

    assert started == []


def test_cleanup_cannot_rollback_a_settled_carry_in_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from core.lesson_carry_in import CarryInFact
    from core.lesson_carry_in_drain import _record_save_drain

    db = _event_db()
    db.commit()
    fact = CarryInFact("lesson", "owner", "a" * 40, "context/rule.md", "case")
    passed = [_call("read_file", {"path": "context/rule.md"}), _call("plan_update", {"operation": "mark_done"})]
    warnings: list[str] = []

    class Logger:
        def opt(self, **_kwargs):
            return self

        def warning(self, message: str):
            warnings.append(message)

    class CleanupAdapter(Adapter):
        def __init__(self, *_args):
            super().__init__([passed, passed, passed])

        def cleanup(self):
            from infra.database import get_db

            cleanup_db = get_db()
            cleanup_db.execute("UPDATE threads SET updated_at='cleanup' WHERE id='owner'")
            cleanup_db.rollback()
            cleanup_db.close()

    class NoCloseDb:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    monkeypatch.setattr("infra.database.get_db", lambda: NoCloseDb())
    monkeypatch.setattr("core.lesson_carry_in_drain._matching_facts", lambda *_args: ([fact], []))
    monkeypatch.setattr("core.lesson_carry_in_drain._fresh_scope", lambda *_args: None)
    monkeypatch.setattr("core.lesson_carry_in_drain.LocalTraceAdapter", CleanupAdapter)
    monkeypatch.setattr("core.lesson_carry_in_drain.logger", Logger())
    monkeypatch.setattr(
        "core.lesson_carry_in._case_for_fact",
        lambda *_args: {"what_happened": "The rule was missed.", "actions": ACTIONS},
    )
    monkeypatch.setattr("core.lesson_carry_in._target_snapshot", lambda *_args: ("target-hash", None))
    monkeypatch.setattr("core.lesson_carry_in.target_scope_owner", lambda *_args: "owner")

    _record_save_drain(tmp_path, [tmp_path / "context" / "rule.md"], "a" * 40)

    assert warnings == []
    assert db.execute("SELECT event_name FROM outcome_events WHERE event_name='learning_loop.carry_in_result'").fetchone()


def test_drain_facts_cleans_up_when_the_adapter_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from core.lesson_carry_in import CarryInFact, drain_facts, record_pending

    db = _event_db()
    fact = CarryInFact("lesson", "owner", "a" * 40, "context/rule.md", "case")
    assert record_pending(db, fact)
    db.commit()
    cleaned: list[bool] = []

    class RaisingAdapter:
        def ready(self) -> bool:
            return True

        def run(self, **_kwargs):
            raise RuntimeError("fresh turn failed")

        def cleanup(self):
            cleaned.append(True)

    monkeypatch.setattr(
        "core.lesson_carry_in._case_for_fact",
        lambda *_args: {"what_happened": "The rule was missed.", "actions": ACTIONS},
    )
    monkeypatch.setattr("core.lesson_carry_in._target_snapshot", lambda *_args: ("target-hash", None))

    with pytest.raises(RuntimeError, match="fresh turn failed"):
        drain_facts(db, tmp_path, [fact], RaisingAdapter())

    assert cleaned == [True]


def test_save_commits_pending_before_any_model_turn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from core.lesson_carry_in import CarryInFact
    from core.lesson_carry_in_drain import _drain_committed_facts

    path = str(tmp_path / "events.db")
    db = _event_db(path)
    fact = CarryInFact("lesson", "owner", "a" * 40, "context/rule.md", "case")
    gone = CarryInFact("other", "vanished-owner", "a" * 40, "context/rule.md", "case")
    seen: list[list[str]] = []

    class WitnessAdapter(Adapter):
        def run(self, **kwargs):
            reader = sqlite3.connect(path)
            try:
                seen.append([row[0] for row in reader.execute(
                    "SELECT event_name FROM outcome_events ORDER BY event_name"
                )])
            finally:
                reader.close()
            return super().run(**kwargs)

    failed = [_call("read_file", {"path": "context/rule.md"})]
    monkeypatch.setattr("core.lesson_carry_in_drain._matching_facts", lambda *_args: ([fact, gone], []))
    monkeypatch.setattr("core.lesson_carry_in_drain._fresh_scope", lambda *_args: None)
    monkeypatch.setattr(
        "core.lesson_carry_in._case_for_fact",
        lambda *_args: {"what_happened": "The rule was missed.", "actions": ACTIONS},
    )
    monkeypatch.setattr("core.lesson_carry_in._target_snapshot", lambda *_args: ("target-hash", None))
    monkeypatch.setattr("core.lesson_carry_in.target_scope_owner", lambda *_args: "owner")

    _drain_committed_facts(
        db, tmp_path, [tmp_path / "context" / "rule.md"], "a" * 40,
        adapter_factory=lambda _user: WitnessAdapter([failed, failed, failed]),
    )

    # A second connection sees both pendings and the no-user retirement before the first sample.
    assert seen[0] == [
        "learning_loop.carry_in_pending", "learning_loop.carry_in_pending", "learning_loop.carry_in_retirement",
    ]
    assert len(seen) == 3


def test_rebaseline_cleanup_cannot_rollback_its_settled_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from core.lesson_carry_in import CarryInFact, CarryInRun, TraceScore, _rebaseline_first, record_result

    db = _event_db()
    previous = CarryInFact("lesson", "owner", "a" * 40, "context/rule.md", "case")
    fact = CarryInFact("lesson", "owner", "b" * 40, "context/rule.md", "case")
    passed_score = TraceScore("pass", "ok", 1)
    assert record_result(db, previous, CarryInRun("settled", (passed_score,) * 3, "selector", "old"))
    db.commit()
    passed = [_call("read_file", {"path": "context/rule.md"}), _call("plan_update", {"operation": "mark_done"})]

    class RebaselineAdapter(Adapter):
        def __init__(self):
            super().__init__([passed, passed, passed])
            self.target = None

        def current_model_identity(self):
            return "new"

        def set_fenced_target(self, target):
            self.target = target

        def cleanup(self):
            from infra.database import get_db

            cleanup_db = get_db()
            cleanup_db.execute("UPDATE threads SET updated_at='cleanup' WHERE id='owner'")
            cleanup_db.rollback()
            cleanup_db.close()

    class NoCloseDb:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    adapter = RebaselineAdapter()
    monkeypatch.setattr("infra.database.get_db", lambda: NoCloseDb())
    monkeypatch.setattr("core.lesson_carry_in._recorded_target_blob", lambda *_args: (b"previous rule", None))
    monkeypatch.setattr(
        "core.lesson_carry_in._case_for_fact",
        lambda *_args: {"what_happened": "The rule was missed.", "actions": ACTIONS},
    )

    runs = []
    assert _rebaseline_first(db, tmp_path, fact, ACTIONS, adapter, runs)

    assert adapter.target is not None
    assert [run.verdict for run in runs] == ["pass"]
    assert db.execute("SELECT event_name FROM outcome_events WHERE subject_key=?", (previous.subject_key + ":rebaseline",)).fetchone()


def test_committed_save_returns_before_its_background_drain_finishes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from core.lesson_carry_in_drain import join_drain_threads, record_committed_save

    started = threading.Event()
    release = threading.Event()

    def blocked_drain(*_args) -> None:
        started.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr("core.lesson_carry_in_drain._record_save_drain", blocked_drain)
    record_committed_save(tmp_path, [tmp_path / "context" / "rule.md"], "a" * 40)

    assert started.wait(timeout=5)
    release.set()
    join_drain_threads()


def test_busy_save_stays_pending_until_the_existing_pending_drain_trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from core.lesson_carry_in import CarryInFact, is_pending
    from core.lesson_carry_in_drain import _drain_pending, record_committed_save
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
    fact = CarryInFact("12345678-1234-5678-1234-567812345678", "owner", "a" * 40, "context/rule.md", ".threads/owner/skills/lesson/SKILL.md#corsia.lesson.case")
    db = _event_db()
    passed = [_call("read_file", {"path": "context/rule.md"}), _call("plan_update", {"operation": "mark_done"})]

    class BusyAdapter(Adapter):
        def __init__(self, *_args):
            super().__init__([], is_ready=False)

    class ImmediateThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

        def join(self):
            return None

    class NoCloseDb:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    monkeypatch.setattr("core.lesson_carry_in._target_snapshot", lambda *_args: ("target-hash", None))
    monkeypatch.setattr("core.lesson_carry_in_drain.LocalTraceAdapter", BusyAdapter)
    monkeypatch.setattr("core.lesson_carry_in_drain._fresh_scope", lambda *_args: None)
    monkeypatch.setattr("core.lesson_carry_in_drain.threading.Thread", ImmediateThread)
    monkeypatch.setattr("infra.database.get_db", lambda: NoCloseDb())
    record_committed_save(workspace, [target], "a" * 40)
    assert is_pending(db, fact)

    class ReadyAdapter(Adapter):
        def __init__(self, *_args):
            super().__init__([passed, passed, passed])

    monkeypatch.setattr("core.lesson_carry_in_drain._pending_facts", lambda _db: [fact])
    monkeypatch.setattr("core.lesson_carry_in_drain.LocalTraceAdapter", ReadyAdapter)
    monkeypatch.setattr("core.lesson_carry_in_drain._fresh_scope", lambda *_args: None)
    monkeypatch.setattr("infra.database.get_db", lambda: NoCloseDb())
    _drain_pending(workspace)

    assert not is_pending(db, fact)


def test_failed_thread_target_does_not_create_an_owner_failure_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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

    monkeypatch.setattr("core.lesson_carry_in._target_snapshot", lambda *_args: ("target-hash", None))
    assert record_pending(db, fact)
    db.commit()
    assert [run.verdict for run in drain_facts(db, workspace, [fact], Adapter([failed, failed, failed]))] == ["fail"]
    assert drain_facts(db, workspace, [fact], Adapter([failed, failed, failed])) == []
    rows = db.execute("SELECT attributes FROM outcome_events WHERE event_name='measurement.correction'").fetchall()
    assert rows == []
    result = db.execute("SELECT agent_thread_id FROM outcome_events WHERE event_name='learning_loop.carry_in_result'").fetchone()
    assert result["agent_thread_id"] is None


def test_pending_result_and_retirement_are_queryable_outcome_events():
    from core.lesson_carry_in import CarryInFact, CarryInRun, TraceScore, is_pending, record_pending, record_result, record_retirement

    db = _event_db()
    fact = CarryInFact("lesson-1", "owner", "a" * 40, "context/rule.md", ".threads/owner/skills/lesson/SKILL.md#corsia.lesson.case")
    assert record_pending(db, fact)
    assert not record_pending(db, fact, reason="local model unavailable")
    assert is_pending(db, fact)
    assert db.execute(
        "SELECT COUNT(*) FROM outcome_events WHERE event_name='learning_loop.carry_in_pending'"
    ).fetchone()[0] == 1

    run = CarryInRun(
        "settled", (TraceScore("pass", "required decisive action", 0),) * 3,
        target_hash="target-sha", delivered_blob_sha="delivered-sha",
    )
    assert record_result(db, fact, run, case_sha="case-sha")
    assert not is_pending(db, fact)
    attrs = json.loads(db.execute(
        "SELECT attributes FROM outcome_events WHERE event_name='learning_loop.carry_in_result'"
    ).fetchone()["attributes"])
    assert attrs["case_sha"] == "case-sha"
    assert attrs["blob_sha"] == "delivered-sha"

    retired = CarryInFact("lesson-2", "owner", "b" * 40, "context/rule.md", ".threads/owner/skills/lesson/SKILL.md#corsia.lesson.case")
    assert record_pending(db, retired)
    assert record_retirement(db, retired, "superseded by a corrected rule")
    assert not is_pending(db, retired)
    row = db.execute("SELECT attributes FROM outcome_events WHERE event_name='learning_loop.carry_in_retirement'").fetchone()
    assert 'superseded by a corrected rule' in row["attributes"]


def test_pending_round_trip_recovers_owner_and_drains_without_a_cap_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from core.lesson_carry_in import CarryInFact, record_pending
    from core.lesson_carry_in_drain import _drain_pending, _pending_facts

    db = _event_db()
    fact = CarryInFact("lesson", "owner", "a" * 40, "context/rule.md", "case")
    assert record_pending(db, fact)
    assert _pending_facts(db) == [fact]
    drained: list[CarryInFact] = []

    class NoCloseDb:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    class Adapter:
        def __init__(self, *_args):
            pass

    def drain(_db, _workspace, facts, _adapter):
        drained.extend(facts)

    monkeypatch.setattr("infra.database.get_db", lambda: NoCloseDb())
    monkeypatch.setattr("core.lesson_carry_in_drain.LocalTraceAdapter", Adapter)
    monkeypatch.setattr("core.lesson_carry_in_drain._fresh_scope", lambda *_args: None)
    monkeypatch.setattr("core.lesson_carry_in_drain._case_for_fact", lambda *_args: {"actions": ACTIONS})
    monkeypatch.setattr("core.lesson_carry_in_drain._target_snapshot", lambda *_args: ("target", None))
    monkeypatch.setattr("core.lesson_carry_in_drain.drain_facts", drain)

    _drain_pending(tmp_path)
    _drain_pending(tmp_path)

    assert drained == [fact, fact]
    assert db.execute(
        "SELECT COUNT(*) FROM outcome_events WHERE event_name='learning_loop.carry_in_pending'"
    ).fetchone()[0] == 1


def test_pending_drain_retires_caseless_first_fact_before_running_later_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from core.lesson_carry_in import CarryInFact, is_pending, record_pending
    from core.lesson_carry_in_drain import _drain_pending

    db = _event_db()
    first = CarryInFact("first", "owner", "a" * 40, "context/first.md", "missing-case")
    second = CarryInFact("second", "owner", "b" * 40, "context/second.md", "case")
    assert record_pending(db, first)
    assert record_pending(db, second)
    drained: list[CarryInFact] = []

    monkeypatch.setattr("infra.database.get_db", lambda: _NoCloseDb(db))
    monkeypatch.setattr("core.lesson_carry_in_drain._pending_facts", lambda _db: [first, second])
    monkeypatch.setattr(
        "core.lesson_carry_in_drain._case_for_fact",
        lambda _workspace, fact: None if fact == first else {"actions": ACTIONS},
    )
    monkeypatch.setattr("core.lesson_carry_in_drain._target_snapshot", lambda *_args: ("target", None))
    monkeypatch.setattr("core.lesson_carry_in_drain._owner_user_id", lambda *_args: "user")
    monkeypatch.setattr("core.lesson_carry_in_drain._fresh_scope", lambda *_args: None)
    monkeypatch.setattr("core.lesson_carry_in_drain.LocalTraceAdapter", lambda *_args: object())
    monkeypatch.setattr("core.lesson_carry_in_drain.drain_facts", lambda _db, _workspace, facts, _adapter: drained.extend(facts))

    _drain_pending(tmp_path)

    assert drained == [second]
    assert not is_pending(db, first)
    assert is_pending(db, second)
    assert "case is unavailable" in db.execute(
        "SELECT attributes FROM outcome_events WHERE event_name='learning_loop.carry_in_retirement'"
    ).fetchone()["attributes"]


def test_pending_drain_retires_missing_owner_before_running_later_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from core.lesson_carry_in import CarryInFact, is_pending, record_pending
    from core.lesson_carry_in_drain import _drain_pending

    db = _event_db()
    first = CarryInFact("first", "vanished-owner", "a" * 40, "context/first.md", "case")
    second = CarryInFact("second", "owner", "b" * 40, "context/second.md", "case")
    assert record_pending(db, first)
    assert record_pending(db, second)
    drained: list[CarryInFact] = []

    monkeypatch.setattr("infra.database.get_db", lambda: _NoCloseDb(db))
    monkeypatch.setattr("core.lesson_carry_in_drain._pending_facts", lambda _db: [first, second])
    monkeypatch.setattr("core.lesson_carry_in_drain._case_for_fact", lambda *_args: {"actions": ACTIONS})
    monkeypatch.setattr("core.lesson_carry_in_drain._target_snapshot", lambda *_args: ("target", None))
    monkeypatch.setattr("core.lesson_carry_in_drain._fresh_scope", lambda *_args: None)
    monkeypatch.setattr("core.lesson_carry_in_drain.LocalTraceAdapter", lambda *_args: object())
    monkeypatch.setattr("core.lesson_carry_in_drain.drain_facts", lambda _db, _workspace, facts, _adapter: drained.extend(facts))

    _drain_pending(tmp_path)

    assert drained == [second]
    assert not is_pending(db, first)
    assert "owner has no user" in db.execute(
        "SELECT attributes FROM outcome_events WHERE event_name='learning_loop.carry_in_retirement'"
    ).fetchone()["attributes"]


def test_pending_drain_retires_target_changed_first_fact_before_running_later_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from core.lesson_carry_in import CarryInFact, is_pending, record_pending
    from core.lesson_carry_in_drain import _drain_pending

    db = _event_db()
    first = CarryInFact("first", "owner", "a" * 40, "context/first.md", "case")
    second = CarryInFact("second", "owner", "b" * 40, "context/second.md", "case")
    assert record_pending(db, first)
    assert record_pending(db, second)
    drained: list[CarryInFact] = []

    monkeypatch.setattr("infra.database.get_db", lambda: _NoCloseDb(db))
    monkeypatch.setattr("core.lesson_carry_in_drain._pending_facts", lambda _db: [first, second])
    monkeypatch.setattr("core.lesson_carry_in_drain._case_for_fact", lambda *_args: {"actions": ACTIONS})
    monkeypatch.setattr(
        "core.lesson_carry_in_drain._target_snapshot",
        lambda _workspace, fact: ("target", "target_changed_after_commit") if fact == first else ("target", None),
    )
    monkeypatch.setattr("core.lesson_carry_in_drain._fresh_scope", lambda *_args: None)
    monkeypatch.setattr("core.lesson_carry_in_drain.LocalTraceAdapter", lambda *_args: object())
    monkeypatch.setattr("core.lesson_carry_in_drain.drain_facts", lambda _db, _workspace, facts, _adapter: drained.extend(facts))

    _drain_pending(tmp_path)

    assert drained == [second]
    assert not is_pending(db, first)
    assert is_pending(db, second)


def test_nonunique_or_cloud_ready_rows_do_not_admit_a_local_model(monkeypatch: pytest.MonkeyPatch):
    from core.lesson_carry_in_contract import ready_local_target

    calls: list[str] = []
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target", lambda model: calls.append(model),
    )
    monkeypatch.setattr(
        "llm.model_status.get_model_status_rows",
        lambda: [{"status": "ready", "provider": "cloud", "is_local": False, "litellm_model": "cloud/model"}],
    )
    assert ready_local_target() is None
    monkeypatch.setattr(
        "llm.model_status.get_model_status_rows",
        lambda: [
            {"status": "ready", "provider": "omlx", "is_local": True, "litellm_model": "omlx/one"},
            {"status": "ready", "provider": "omlx", "is_local": True, "litellm_model": "omlx/two"},
        ],
    )
    assert ready_local_target() is None
    assert calls == []


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


def _carry_in_run(verdict: str):
    from core.lesson_carry_in import CarryInRun, TraceScore

    return CarryInRun("settled", (TraceScore(verdict, "test", None),) * 3)


def _drain_edit_after_prior(
    db, monkeypatch: pytest.MonkeyPatch, prior_verdict: str, *, prior_role: str = "edit", changed_case: bool = False,
):
    from core.lesson_carry_in import CarryInFact, _case_sha, drain_facts, record_pending, record_result

    prior_case = {"what_happened": "Missed.", "right_call": "Read it.", "does_not_apply": "Draft.", "actions": ACTIONS}
    edit_case = {**prior_case, "right_call": "Read the changed rule."} if changed_case else prior_case
    prior = CarryInFact("lesson", "owner", "a" * 40, "context/rule.md", "case", prior_role)
    edit = CarryInFact("lesson", "owner", "b" * 40, "context/rule.md", "case", "edit")
    assert record_result(
        db, prior, _carry_in_run(prior_verdict), case_sha=_case_sha(prior_case),
        case_status="proven" if prior_verdict == "pass" else "unproven",
    )
    assert record_pending(db, edit)
    db.commit()
    monkeypatch.setattr("core.lesson_carry_in._case_for_fact", lambda *_args: edit_case)
    monkeypatch.setattr("core.lesson_carry_in._target_snapshot", lambda *_args: ("target", None))
    monkeypatch.setattr("core.lesson_carry_in.target_scope_owner", lambda *_args: "owner")
    failed = [_call("read_file", {"path": "context/rule.md"})]

    assert [run.verdict for run in drain_facts(db, Path("workspace"), [edit], Adapter([failed, failed, failed]))] == ["fail"]
    return edit


def _corrections(db):
    return db.execute(
        "SELECT agent_thread_id,attributes FROM outcome_events WHERE event_name='measurement.correction'"
    ).fetchall()


def test_prior_case_status_rejects_a_pass_recorded_after_this_pending_fact():
    from core.lesson_carry_in import CarryInFact, _case_sha, record_pending, record_result
    from core.lesson_carry_in_result_queries import prior_case_status

    db = _event_db()
    case = {"what_happened": "Missed.", "right_call": "Read it.", "does_not_apply": "Draft.", "actions": ACTIONS}
    older = CarryInFact("lesson", "owner", "a" * 40, "context/rule.md", "case")
    newer = CarryInFact("lesson", "owner", "b" * 40, "context/rule.md", "case")
    assert record_pending(db, older)
    assert record_result(db, newer, _carry_in_run("pass"), case_sha=_case_sha(case), case_status="proven")

    assert prior_case_status(db, older, _case_sha(case)) == "unproven"


def test_fail_then_fail_marks_a_case_unproven_without_a_correction(monkeypatch: pytest.MonkeyPatch):
    db = _event_db()

    edit = _drain_edit_after_prior(db, monkeypatch, "fail")

    attrs = json.loads(db.execute(
        "SELECT attributes FROM outcome_events WHERE subject_key=? AND event_name='learning_loop.carry_in_result'",
        (edit.subject_key,),
    ).fetchone()["attributes"])
    assert attrs["case_status"] == "unproven"
    assert _corrections(db) == []


def test_pass_then_fail_records_one_correction_for_the_target_owner(monkeypatch: pytest.MonkeyPatch):
    db = _event_db()

    _drain_edit_after_prior(db, monkeypatch, "pass")

    rows = _corrections(db)
    assert len(rows) == 1
    assert rows[0]["agent_thread_id"] == "owner"
    assert json.loads(rows[0]["attributes"])["source_id"].startswith("carry_in_regression:")


def test_changed_case_sha_resets_a_prior_pass_to_unproven(monkeypatch: pytest.MonkeyPatch):
    db = _event_db()

    edit = _drain_edit_after_prior(db, monkeypatch, "pass", changed_case=True)

    attrs = json.loads(db.execute(
        "SELECT attributes FROM outcome_events WHERE subject_key=? AND event_name='learning_loop.carry_in_result'",
        (edit.subject_key,),
    ).fetchone()["attributes"])
    assert attrs["case_status"] == "unproven"
    assert _corrections(db) == []


def test_rebaseline_pass_then_edit_fail_records_one_correction(monkeypatch: pytest.MonkeyPatch):
    db = _event_db()

    _drain_edit_after_prior(db, monkeypatch, "pass", prior_role="rebaseline")

    assert len(_corrections(db)) == 1
