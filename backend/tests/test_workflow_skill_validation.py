"""Validation contracts for workflow skill frontmatter."""

from __future__ import annotations

import pytest

from core.skill_validation import validate_skill_bundle, validate_skill_file, validate_skill_text
from infra.system_package import system_package_dir
from tests.skill_sidecar_helpers import POINTER


def _validate(steps: str):
    sidecar = f"handler: {{kind: instruction}}\nworkflow:\n  steps:\n{steps}"
    skill = f"---\nname: workflow-test\ndescription: Test workflow metadata\n{POINTER}---\n# Workflow test\n"
    return validate_skill_bundle(skill, {"corsia.yaml": sidecar})


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ("      - id: build\n        required: true\n", "check is required"),
        ("      - id: build\n        after: [missing]\n", "references unknown step"),
        (
            "      - id: build\n        after: [review]\n"
            "      - id: review\n        after: [build]\n",
            "has a cycle",
        ),
        ("      - id: build\n        models: {best: model}\n", "models is reserved"),
        ("      - id: build\n        unexpected: value\n", "unknown key"),
        (
            "      - id: build\n        difficulty: {default: impossible}\n",
            "difficulty.default must be one of routine, standard, hard",
        ),
        (
            "      - id: build\n        quality: malformed\n",
            "quality must be a list of mappings",
        ),
        (
            "      - id: build\n        instruction: Drafting\n",
            "instruction must be a SKILL.md heading",
        ),
    ],
)
def test_workflow_steps_refuse_uncheckable_or_unresolvable_metadata(steps, message):
    result = _validate(steps)

    assert not result.ok
    assert any(message in error for error in result.errors)


def test_workflow_steps_accept_required_checks_and_known_dependencies():
    result = _validate(
        "      - id: product\n"
        "        required: true\n"
        "        check: Approved behavior is recorded\n"
        "      - id: build\n"
        "        after: [product]\n"
    )

    assert result.ok


def test_packaged_declared_workflow_contract_survives_system_cut():
    package = system_package_dir()
    decision = validate_skill_file(package / "skills/decision-brief/SKILL.md")
    task = validate_skill_text(
        (package / "skills/task-discipline/SKILL.md").read_text(encoding="utf-8")
    )

    assert decision.ok, decision.errors
    steps = decision.corsia["workflow"]["steps"]
    assert [step["id"] for step in steps] == [
        "product-chain", "long-form", "brief", "decision",
    ]
    assert "Applying these steps" in decision.body
    for contract in (
        "close_node",
        "workflow_ref",
        "workflow_step",
        "+<short-name>",
        "difficulty_rerated",
    ):
        assert contract in task.body
