from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from inspect_ai.model import ModelOutput

from eval.skill_evals import (
    _MAX_CAPTURED_OUTPUT_BYTES,
    _MAX_CAPTURED_OUTPUT_FILES,
    _TRUNCATED_OUTPUT_MARKER,
    _read_workspace_outputs,
    SkillEvalFormatError,
    build_skill_eval_suite,
)


def _write_skill(
    root: Path,
    *,
    expectations_key: str = "assertions",
) -> Path:
    skill = root / "example-skill"
    (skill / "evals" / "files").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Example skill\n", encoding="utf-8")
    (skill / "evals" / "files" / "input.txt").write_text("input", encoding="utf-8")
    case = {
        "id": 7,
        "kind": "help",
        "prompt": "Summarize the input file.",
        "expected_output": "A concise summary.",
        "files": ["evals/files/input.txt"],
        expectations_key: ["Mentions the input", "Writes an output"],
    }
    (skill / "evals" / "evals.json").write_text(
        json.dumps({"skill_name": "example-skill", "evals": [case]}),
        encoding="utf-8",
    )
    return skill


def _samples(suite):
    return list(suite.task.dataset)


def _message_content(message) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)


def test_loads_open_evals_into_isolated_arm_samples_with_selected_skill_context(tmp_path):
    skill = _write_skill(tmp_path)

    suite = build_skill_eval_suite(skill)

    assert suite.skill_name == "example-skill"
    assert suite.cases[0].case_id == "7"
    without, with_skill = _samples(suite)
    assert [sample.id for sample in (without, with_skill)] == ["7:without", "7:with"]
    assert without.metadata["skill_eval"]["workspace"] == "workspace"
    assert "workspace/skill/SKILL.md" not in without.files
    assert "workspace/skill/SKILL.md" in with_skill.files
    assert "workspace/inputs/evals/files/input.txt" in with_skill.files
    assert len(without.input) == 1
    assert len(with_skill.input) == 2
    assert "Example skill" not in _message_content(without.input[0])
    assert "Example skill" in _message_content(with_skill.input[0])
    assert "Assertions to grade:" in with_skill.target
    assert suite.task.sandbox.type == "local"


def test_accepts_assertions_and_expectations_without_duplicate_grading(tmp_path):
    skill = _write_skill(tmp_path)
    eval_path = skill / "evals" / "evals.json"
    payload = json.loads(eval_path.read_text(encoding="utf-8"))
    payload["evals"][0]["expectations"] = ["Writes an output", "Uses the source"]
    eval_path.write_text(json.dumps(payload), encoding="utf-8")

    suite = build_skill_eval_suite(skill)

    assert suite.cases[0].expectations == (
        "Mentions the input",
        "Writes an output",
        "Uses the source",
    )


def test_rejects_case_ids_that_collide_after_string_normalization(tmp_path):
    skill = _write_skill(tmp_path)
    eval_path = skill / "evals" / "evals.json"
    payload = json.loads(eval_path.read_text(encoding="utf-8"))
    duplicate = dict(payload["evals"][0])
    duplicate["id"] = "7"
    payload["evals"].append(duplicate)
    eval_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        SkillEvalFormatError,
        match=r"evals\[1\]\.id duplicates .*evals\[0\]\.id after string normalization",
    ):
        build_skill_eval_suite(skill)


def test_old_new_arms_stage_their_own_skill_contents_and_context(tmp_path):
    current = _write_skill(tmp_path / "current")
    baseline = _write_skill(tmp_path / "baseline")
    (baseline / "SKILL.md").write_text("# Previous skill\n", encoding="utf-8")

    suite = build_skill_eval_suite(
        current,
        arm_mode="old_new",
        baseline_skill_dir=baseline,
    )

    old, new = _samples(suite)
    assert old.id == "7:old"
    assert new.id == "7:new"
    assert old.files["workspace/skill/SKILL.md"] == str(baseline / "SKILL.md")
    assert new.files["workspace/skill/SKILL.md"] == str(current / "SKILL.md")
    assert "Previous skill" in _message_content(old.input[0])
    assert "Example skill" in _message_content(new.input[0])


@pytest.mark.parametrize("target", ["current", "baseline"])
def test_rejects_skill_symlink_that_escapes_its_declared_folder(tmp_path, target):
    external = tmp_path / "external-skill.md"
    external.write_text("outside", encoding="utf-8")
    current = _write_skill(tmp_path / "current")
    baseline = _write_skill(tmp_path / "baseline")
    chosen = current if target == "current" else baseline
    (chosen / "SKILL.md").unlink()
    (chosen / "SKILL.md").symlink_to(external)

    with pytest.raises(SkillEvalFormatError, match="escapes the skill folder"):
        build_skill_eval_suite(
            current,
            arm_mode="old_new",
            baseline_skill_dir=baseline,
        )


def test_rejects_evals_json_symlink_that_escapes_the_skill_folder(tmp_path):
    external = tmp_path / "external-evals.json"
    external.write_text(json.dumps({"skill_name": "external", "evals": []}), encoding="utf-8")
    skill = _write_skill(tmp_path)
    evals_file = skill / "evals" / "evals.json"
    evals_file.unlink()
    evals_file.symlink_to(external)

    with pytest.raises(SkillEvalFormatError, match="escapes the skill folder"):
        build_skill_eval_suite(skill)


def test_bounds_captured_workspace_output_count_and_bytes():
    class FakeSandbox:
        async def exec(self, command, **_kwargs):
            if command[0] == "sh":
                return SimpleNamespace(
                    stdout="\n".join(
                        f"workspace/outputs/{index}.txt"
                        for index in range(_MAX_CAPTURED_OUTPUT_FILES + 1)
                    ),
                    returncode=0,
                )
            raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(RuntimeError, match="more than"):
        asyncio.run(_read_workspace_outputs(FakeSandbox()))

    class BoundedFakeSandbox:
        async def exec(self, command, **_kwargs):
            if command[0] == "sh":
                return SimpleNamespace(
                    stdout="workspace/outputs/result.txt",
                    returncode=0,
                )
            assert command[:2] == ["head", "-c"]
            assert int(command[2]) <= _MAX_CAPTURED_OUTPUT_BYTES
            return SimpleNamespace(
                stdout="x" * (int(command[2]) + 1),
                returncode=0,
            )

    captured = asyncio.run(_read_workspace_outputs(BoundedFakeSandbox()))
    assert len(captured) == 1
    assert captured[0]["content"].endswith(_TRUNCATED_OUTPUT_MARKER)
    assert len(captured[0]["content"].encode("utf-8")) <= _MAX_CAPTURED_OUTPUT_BYTES


def test_output_capture_fails_when_listing_command_fails():
    class FailingListingSandbox:
        async def exec(self, command, **_kwargs):
            assert command[:2] == ["sh", "-c"]
            assert "set -e" in command[2]
            assert "find workspace/outputs -type f -print > \"$listing\"" in command[2]
            assert "|" not in command[2]
            return SimpleNamespace(stdout="", stderr="find failed", returncode=23)

    with pytest.raises(RuntimeError, match=r"output listing.*exit 23.*find failed"):
        asyncio.run(_read_workspace_outputs(FailingListingSandbox()))


def test_output_capture_fails_when_file_read_command_fails():
    class FailingReadSandbox:
        async def exec(self, command, **_kwargs):
            if command[0] == "sh":
                return SimpleNamespace(
                    stdout="workspace/outputs/result.txt",
                    returncode=0,
                )
            return SimpleNamespace(stdout="", stderr="read failed", returncode=1)

    with pytest.raises(
        RuntimeError,
        match=r"output read for workspace/outputs/result\.txt.*exit 1.*read failed",
    ):
        asyncio.run(_read_workspace_outputs(FailingReadSandbox()))


def test_two_arm_local_mockllm_smoke_captures_outputs_and_isolates_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "inspect-data"))
    skill = _write_skill(tmp_path)
    from eval.runner import run_skill_evals

    without_command = (
        "test ! -e workspace/skill/SKILL.md && "
        "test \"$(cat workspace/inputs/evals/files/input.txt)\" = input && "
        "mkdir -p workspace/outputs && printf without > workspace/outputs/result.txt"
    )
    with_command = (
        "test -f workspace/skill/SKILL.md && "
        "grep -q 'Example skill' workspace/skill/SKILL.md && "
        "test \"$(cat workspace/inputs/evals/files/input.txt)\" = input && "
        "mkdir -p workspace/outputs && printf with > workspace/outputs/result.txt"
    )
    def scripted_output(messages, _tools, _choice, _config):
        if any(type(message).__name__ == "ChatMessageTool" for message in messages):
            return ModelOutput.from_content("mockllm", "complete")
        is_with_skill = "Example skill" in str(messages)
        command = with_command if is_with_skill else without_command
        return ModelOutput.for_tool_call("mockllm", "bash", {"command": command})

    logs = run_skill_evals(
        skill,
        model="mockllm/model",
        model_args={"custom_outputs": scripted_output},
        log_dir=str(tmp_path / "logs"),
        max_samples=1,
        score=False,
    )

    samples = {sample.id: sample for sample in logs[0].samples}
    assert set(samples) == {"7:without", "7:with"}
    assert "Example skill" not in str(samples["7:without"].input)
    assert "Example skill" in str(samples["7:with"].input)
    assert "Workspace outputs:" in samples["7:without"].output.completion
    assert "workspace/outputs/result.txt:\nwithout" in samples["7:without"].output.completion
    assert "workspace/outputs/result.txt:\nwith" in samples["7:with"].output.completion


def test_runner_dispatches_native_skill_suite_through_existing_inspect_path(tmp_path, monkeypatch):
    skill = _write_skill(tmp_path)
    seen = {}

    def fake_eval(task, **kwargs):
        seen["task"] = task
        seen["kwargs"] = kwargs
        return ["fixture-log"]

    monkeypatch.setattr("eval.runner.ia_eval", fake_eval)
    from eval.runner import run_skill_evals

    logs = run_skill_evals(
        skill,
        model="fixture-model",
        judge_model="fixture-judge",
        log_dir=str(tmp_path / "logs"),
    )

    assert logs == ["fixture-log"]
    assert seen["task"].name == "skill_eval_example-skill"
    assert seen["kwargs"]["model"] == "fixture-model"
    assert seen["kwargs"]["model_roles"] == {
        "grader": "fixture-judge",
        "judge": "fixture-judge",
    }


@pytest.mark.parametrize("judge_model", [None, "   "])
def test_runner_refuses_model_graded_skill_evals_without_explicit_judge(
    tmp_path,
    monkeypatch,
    judge_model,
):
    skill = _write_skill(tmp_path)
    monkeypatch.setattr(
        "eval.runner.ia_eval",
        lambda *_args, **_kwargs: pytest.fail("Inspect must not run without a judge"),
    )
    from eval.runner import run_skill_evals

    with pytest.raises(ValueError, match="require an explicit non-empty judge_model"):
        run_skill_evals(
            skill,
            model="fixture-model",
            judge_model=judge_model,
            log_dir=str(tmp_path / "logs"),
        )


def test_runner_binds_explicit_judge_to_model_graded_scorer(tmp_path, monkeypatch):
    skill = _write_skill(tmp_path)
    from eval import skill_evals
    from eval.runner import run_skill_evals

    seen = {}
    real_model_graded_qa = skill_evals.model_graded_qa

    def tracking_model_graded_qa(*args, **kwargs):
        seen["model"] = kwargs.get("model")
        return real_model_graded_qa(*args, **kwargs)

    monkeypatch.setattr(skill_evals, "model_graded_qa", tracking_model_graded_qa)
    monkeypatch.setattr("eval.runner.ia_eval", lambda *_args, **_kwargs: ["fixture-log"])

    assert run_skill_evals(
        skill,
        model="fixture-model",
        judge_model="fixture-judge",
        log_dir=str(tmp_path / "logs"),
    ) == ["fixture-log"]
    assert seen["model"] == "fixture-judge"


def test_runner_allows_ungraded_skill_evals_without_judge(tmp_path, monkeypatch):
    skill = _write_skill(tmp_path)
    seen = {}

    def fake_eval(task, **kwargs):
        seen["task"] = task
        seen["kwargs"] = kwargs
        return ["fixture-log"]

    monkeypatch.setattr("eval.runner.ia_eval", fake_eval)
    from eval.runner import run_skill_evals

    assert run_skill_evals(
        skill,
        model="fixture-model",
        log_dir=str(tmp_path / "logs"),
        score=False,
    ) == ["fixture-log"]
    assert seen["task"].scorer is None
    assert "model_roles" not in seen["kwargs"]
