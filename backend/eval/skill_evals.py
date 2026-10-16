from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, cast

from inspect_ai import Task
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatMessageSystem, ChatMessageUser
from inspect_ai.scorer import model_graded_qa
from inspect_ai.solver import Generate, Solver, TaskState, generate, solver, use_tools
from inspect_ai.tool import bash
from inspect_ai.util import sandbox


ArmMode = Literal["with_without", "old_new"]
Arm = Literal["with", "without", "old", "new"]
_WORKSPACE = "workspace"
_OUTPUTS = f"{_WORKSPACE}/outputs"
_MAX_CAPTURED_OUTPUT_FILES = 20
_MAX_CAPTURED_OUTPUT_BYTES = 64 * 1024
_TRUNCATED_OUTPUT_MARKER = "\n[output capture limit reached; content may be truncated]"


@dataclass(frozen=True)
class SkillEvalCase:
    """One open Agent Skills output-quality evaluation case."""

    case_id: str
    prompt: str
    expected_output: str
    files: tuple[Path, ...]
    expectations: tuple[str, ...]
    kind: str | None = None


@dataclass(frozen=True)
class SkillEvalSuite:
    """Parsed fixtures and an Inspect task ready to run, not completed results."""

    skill_name: str
    skill_dir: Path
    cases: tuple[SkillEvalCase, ...]
    task: Task
    arm_mode: ArmMode
    baseline_skill_dir: Path | None


class SkillEvalFormatError(ValueError):
    """Raised when a skill does not contain valid open Agent Skills fixtures."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SkillEvalFormatError(f"Missing required skill eval file: {path}") from error
    except json.JSONDecodeError as error:
        raise SkillEvalFormatError(
            f"Malformed JSON in skill eval file {path}: {error.msg}",
        ) from error


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillEvalFormatError(f"{path} must be a non-empty string")
    return value


def _resolve_in_root(root: Path, relative_path: str, field: str) -> Path:
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise SkillEvalFormatError(f"{field} path escapes the skill folder: {relative_path}") from error
    if not candidate.is_file():
        raise SkillEvalFormatError(f"{field} file is missing: {relative_path}")
    return candidate


def _resolve_fixture(skill_dir: Path, relative_path: Any, field: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise SkillEvalFormatError(f"{field} must contain non-empty relative paths")
    return _resolve_in_root(skill_dir, relative_path, field)


def _expectations(raw_case: Mapping[str, Any], case_path: str) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("assertions", "expectations"):
        raw_values = raw_case.get(key, [])
        if not isinstance(raw_values, list):
            raise SkillEvalFormatError(f"{case_path}.{key} must be a list when provided")
        for index, value in enumerate(raw_values):
            values.append(_require_string(value, f"{case_path}.{key}[{index}]"))
    return tuple(dict.fromkeys(values))


def _load_cases(skill_dir: Path) -> tuple[str, tuple[SkillEvalCase, ...]]:
    eval_file = _resolve_in_root(skill_dir, "evals/evals.json", "evals")
    payload = _read_json(eval_file)
    if not isinstance(payload, dict):
        raise SkillEvalFormatError(f"{eval_file} must contain a JSON object")
    skill_name = _require_string(payload.get("skill_name"), f"{eval_file}.skill_name")
    raw_evals = payload.get("evals")
    if not isinstance(raw_evals, list) or not raw_evals:
        raise SkillEvalFormatError(f"{eval_file}.evals must be a non-empty list")

    cases: list[SkillEvalCase] = []
    seen_ids: dict[str, str] = {}
    for index, raw_case in enumerate(raw_evals):
        case_path = f"{eval_file}.evals[{index}]"
        if not isinstance(raw_case, dict):
            raise SkillEvalFormatError(f"{case_path} must be an object")
        case_id = raw_case.get("id")
        if not isinstance(case_id, (int, str)) or isinstance(case_id, bool):
            raise SkillEvalFormatError(f"{case_path}.id must be a string or integer")
        normalized_case_id = str(case_id)
        if normalized_case_id in seen_ids:
            raise SkillEvalFormatError(
                f"{case_path}.id duplicates {seen_ids[normalized_case_id]}.id "
                f"after string normalization ({normalized_case_id!r})",
            )
        seen_ids[normalized_case_id] = case_path
        raw_files = raw_case.get("files", [])
        if not isinstance(raw_files, list):
            raise SkillEvalFormatError(f"{case_path}.files must be a list when provided")
        cases.append(
            SkillEvalCase(
                case_id=normalized_case_id,
                prompt=_require_string(raw_case.get("prompt"), f"{case_path}.prompt"),
                expected_output=_require_string(
                    raw_case.get("expected_output"), f"{case_path}.expected_output",
                ),
                files=tuple(
                    _resolve_fixture(skill_dir, item, f"{case_path}.files[{file_index}]")
                    for file_index, item in enumerate(raw_files)
                ),
                expectations=_expectations(raw_case, case_path),
                kind=raw_case.get("kind") if isinstance(raw_case.get("kind"), str) else None,
            ),
        )
    return skill_name, tuple(cases)


def _resolve_skill_dir(path: str | Path, field: str) -> tuple[Path, Path]:
    directory = Path(path).resolve()
    if not directory.is_dir():
        raise SkillEvalFormatError(f"{field} must be a skill folder: {directory}")
    return directory, _resolve_in_root(directory, "SKILL.md", field)


def _arm_paths(
    skill_dir: Path,
    skill_file: Path,
    arm_mode: ArmMode,
    baseline_skill_dir: str | Path | None,
) -> tuple[tuple[Arm, Path | None], ...]:
    if arm_mode == "with_without":
        return (("without", None), ("with", skill_file))
    if arm_mode != "old_new":
        raise ValueError("arm_mode must be 'with_without' or 'old_new'")
    if baseline_skill_dir is None:
        raise SkillEvalFormatError("old_new comparisons require baseline_skill_dir")
    _, baseline_skill_file = _resolve_skill_dir(baseline_skill_dir, "baseline_skill_dir")
    return (("old", baseline_skill_file), ("new", skill_file))


def _sample_files(
    case: SkillEvalCase,
    skill_dir: Path,
    skill_file: Path | None,
) -> dict[str, str]:
    files = {
        f"{_WORKSPACE}/inputs/{path.relative_to(skill_dir).as_posix()}": str(path)
        for path in case.files
    }
    if skill_file is not None:
        files[f"{_WORKSPACE}/skill/SKILL.md"] = str(skill_file)
    return files


def _sample_input(case: SkillEvalCase, arm: Arm, skill_file: Path | None) -> list[Any]:
    messages: list[Any] = []
    if skill_file is not None:
        source = "selected" if arm in {"with", "new"} else "baseline"
        messages.append(
            ChatMessageSystem(
                content=(
                    f"The following {source} skill is available for this evaluation. "
                    "Follow it for this task. Its identical text is also staged at "
                    f"{_WORKSPACE}/skill/SKILL.md for tool use.\n\n"
                    f"--- SKILL.md ---\n{skill_file.read_text(encoding='utf-8')}\n--- end SKILL.md ---"
                ),
            ),
        )
    messages.append(
        ChatMessageUser(
            content=(
                "This is a fresh, isolated skill-evaluation workspace. "
                f"Input fixtures, if any, are under {_WORKSPACE}/inputs/. "
                f"Write file outputs only inside {_OUTPUTS}/.\n\n"
                f"User request:\n{case.prompt}"
            ),
        ),
    )
    return messages


def _case_target(case: SkillEvalCase) -> str:
    parts = [f"Expected output:\n{case.expected_output}"]
    if case.expectations:
        parts.append("Assertions to grade:\n" + "\n".join(f"- {item}" for item in case.expectations))
    return "\n\n".join(parts)


def _require_successful_capture(result: Any, command: str) -> None:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"Skill evaluation output capture failed running {command} "
            f"(exit {result.returncode}){suffix}",
        )


async def _read_workspace_outputs(environment: Any) -> list[dict[str, str]]:
    listing = await environment.exec(
        [
            "sh",
            "-c",
            (
                "set -e\n"
                f"if [ -d {_OUTPUTS} ]; then\n"
                "  listing=$(mktemp)\n"
                "  trap 'rm -f \"$listing\"' EXIT\n"
                f"  find {_OUTPUTS} -type f -print > \"$listing\"\n"
                f"  head -n {_MAX_CAPTURED_OUTPUT_FILES + 1} \"$listing\"\n"
                "fi"
            ),
        ],
        timeout=10,
    )
    _require_successful_capture(listing, "output listing")
    paths = [path for path in listing.stdout.splitlines() if path]
    if len(paths) > _MAX_CAPTURED_OUTPUT_FILES:
        raise RuntimeError(
            f"Skill evaluation wrote more than {_MAX_CAPTURED_OUTPUT_FILES} output files",
        )

    remaining = _MAX_CAPTURED_OUTPUT_BYTES
    marker_bytes = len(_TRUNCATED_OUTPUT_MARKER.encode("utf-8"))
    captured = []
    for path in paths:
        read_limit = remaining - marker_bytes
        if read_limit <= 0:
            captured.append(
                {"path": path, "content": _TRUNCATED_OUTPUT_MARKER.lstrip("\n")},
            )
            break
        result = await environment.exec(
            ["head", "-c", str(read_limit), path], timeout=10,
        )
        _require_successful_capture(result, f"output read for {path}")
        raw = result.stdout.encode("utf-8")[:read_limit]
        content = raw.decode("utf-8", errors="replace")
        if len(raw) == read_limit:
            content += _TRUNCATED_OUTPUT_MARKER
            remaining = 0
        else:
            remaining -= len(raw)
        captured.append({"path": path, "content": content})
    return captured


@solver
def _capture_workspace_outputs() -> Solver:
    async def solve(state: TaskState, _: Generate) -> TaskState:
        captured = await _read_workspace_outputs(sandbox())
        state.store.set("skill_eval_outputs", captured)
        if captured:
            rendered = "\n\n".join(
                f"{item['path']}:\n{item['content']}" for item in captured
            )
            state.output.completion = (
                f"{state.output.completion}\n\nWorkspace outputs:\n{rendered}"
            )
        return state

    return cast(Solver, solve)


def build_skill_eval_suite(
    skill_dir: str | Path,
    *,
    arm_mode: ArmMode = "with_without",
    baseline_skill_dir: str | Path | None = None,
    judge_model: str | None = None,
) -> SkillEvalSuite:
    """Build an execution-ready task for trusted in-workspace skills.

    Inspect's local sandbox gives every sample a separate temporary directory;
    it prevents arms from seeing each other's staged files or outputs, but is
    not a security jail. Untrusted community skills are outside phase 1a.
    """

    resolved_skill_dir, skill_file = _resolve_skill_dir(skill_dir, "skill_dir")
    skill_name, cases = _load_cases(resolved_skill_dir)
    arms = _arm_paths(resolved_skill_dir, skill_file, arm_mode, baseline_skill_dir)

    samples: list[Sample] = []
    for case in cases:
        for arm, arm_skill_file in arms:
            samples.append(
                Sample(
                    id=f"{case.case_id}:{arm}",
                    input=_sample_input(case, arm, arm_skill_file),
                    target=_case_target(case),
                    files=_sample_files(case, resolved_skill_dir, arm_skill_file),
                    metadata={
                        "skill_eval": {
                            "skill_name": skill_name,
                            "case_id": case.case_id,
                            "kind": case.kind,
                            "arm": arm,
                            "expectations": list(case.expectations),
                            "workspace": _WORKSPACE,
                        },
                    },
                ),
            )

    task = Task(
        dataset=samples,
        solver=[use_tools(bash(timeout=30)), generate(), _capture_workspace_outputs()],
        scorer=model_graded_qa(model=judge_model) if judge_model is not None else None,
        sandbox="local",
        name=f"skill_eval_{skill_name}",
        metadata={"skill_eval": {"skill_name": skill_name, "arm_mode": arm_mode}},
    )
    baseline_dir = Path(baseline_skill_dir).resolve() if baseline_skill_dir else None
    return SkillEvalSuite(
        skill_name=skill_name,
        skill_dir=resolved_skill_dir,
        cases=cases,
        task=task,
        arm_mode=arm_mode,
        baseline_skill_dir=baseline_dir,
    )


__all__ = [
    "ArmMode",
    "SkillEvalCase",
    "SkillEvalFormatError",
    "SkillEvalSuite",
    "build_skill_eval_suite",
]
