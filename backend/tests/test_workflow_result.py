"""``corsia.workflow.result``: the declared schema, its check of a worker's final message,
the validator reading it with no steps, and the workflow version moving when only it changes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.skill_validation import validate_skill_bundle
from core.workflow_result import ResultInvalid, check_result, schema_errors
from infra import config
from tests.skill_sidecar_helpers import POINTER
from tools.workflow_task_fields import _workflow_version

FINDINGS = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title"],
                "properties": {
                    "title": {"type": "string"},
                    "severity": {
                        "type": "string", "enum": ["blocker", "major", "minor", "nit"], "unknown": "nit"
                    },
                    "line": {"type": "integer", "coerce": True},
                    "blocking": {"type": "boolean", "default": False},
                },
            },
        }
    },
}
SKILL = f"---\nname: worker-flow\ndescription: A worker's workflow.\n{POINTER}---\n# Worker flow\n"
SIDECAR = """handler: {kind: instruction}
workflow:
  result:
    type: object
    required: [findings]
    properties:
      findings: {type: array, items: {type: string}}
"""


def test_a_findings_schema_is_usable():
    assert schema_errors(FINDINGS) == []


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ([], "must be a mapping"),
        ({"type": "date"}, "type must be one of"),
        ({"type": "string", "items": {"type": "string"}}, "items is an unknown key for type string"),
        ({"type": "array"}, "items is required"),
        ({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]}, "required must list names"),
        ({"type": "string", "enum": ["a"], "unknown": "b"}, "unknown must be one of its enum"),
        ({"type": "integer", "coerce": "yes"}, "coerce must be true or false"),
        ({"type": "integer", "default": "x"}, "default must be an integer"),
    ],
)
def test_a_schema_that_cannot_check_a_result_is_refused(schema, message):
    assert any(message in error for error in schema_errors(schema))


def test_a_fenced_payload_is_read_with_defaults_coercions_and_the_unknown_value():
    text = (
        "Done. Here is the result:\n```json\n"
        '{"findings": [{"title": "leak", "severity": "critical", "line": "12"}]}\n```\nThanks.'
    )

    assert check_result(text, FINDINGS) == {
        "findings": [{"title": "leak", "severity": "nit", "line": 12, "blocking": False}]
    }


def test_a_payload_inside_prose_is_found():
    assert check_result('Result: {"findings": []} as asked.', FINDINGS) == {"findings": []}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (None, "no final message"),
        ("   ", "no final message"),
        ("I could not finish.", "no JSON object or array"),
        ('{"other": 1}', "result.findings is required"),
        ('{"findings": [{"title": "x", "line": "twelve"}]}', "result.findings[0].line must be an integer"),
        ('{"findings": [{"title": "x", "blocking": "no"}]}', "must be true or false"),
    ],
)
def test_a_result_that_fails_after_coercion_is_invalid(text, message):
    with pytest.raises(ResultInvalid, match=message.replace("[", r"\[").replace("]", r"\]")):
        check_result(text, FINDINGS)


def test_a_workflow_with_a_result_and_no_steps_is_still_validated():
    bad = SIDECAR.replace("type: string}", "type: text}")

    assert validate_skill_bundle(SKILL, {"corsia.yaml": SIDECAR}).ok
    result = validate_skill_bundle(SKILL, {"corsia.yaml": bad})
    assert not result.ok
    assert any("corsia.workflow.result.properties.findings.items.type" in e for e in result.errors)


def test_a_key_beside_result_is_left_to_its_own_owner():
    # No unknown-key check at corsia.workflow: a later key there (an owner, say) must still load.
    sidecar = SIDECAR.replace("workflow:\n", "workflow:\n  owner: someone\n")
    assert validate_skill_bundle(SKILL, {"corsia.yaml": sidecar}).ok


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo, check=True, capture_output=True,
    )


def test_changing_only_the_result_changes_the_workflow_version(tmp_path, monkeypatch):
    folder = tmp_path / "skills" / "worker-flow"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (folder / "corsia.yaml").write_text(SIDECAR, encoding="utf-8")
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "--quiet", "-m", "first")
    monkeypatch.setattr(config, "WORKSPACE", str(tmp_path))
    skill = SimpleNamespace(name="worker-flow", scope="thread", path=folder / "SKILL.md")
    before = _workflow_version(skill)

    (folder / "corsia.yaml").write_text(SIDECAR.replace("type: string}", "type: integer}"), encoding="utf-8")
    _git(tmp_path, "commit", "--quiet", "-am", "result only")
    after = _workflow_version(skill)

    assert before and after and before != after
    assert before.split("+")[0] == after.split("+")[0]
