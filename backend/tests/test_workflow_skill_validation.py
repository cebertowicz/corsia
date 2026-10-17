"""Validation contracts for workflow skill frontmatter."""

from __future__ import annotations

import pytest

from core.skill_validation import validate_skill_bundle, validate_skill_file, validate_skill_text
from tests.skill_sidecar_helpers import to_sidecar_form
from infra.system_package import system_package_dir


def _skill(steps: str) -> str:
    return f"""---
name: workflow-test
description: Test workflow metadata
corsia:
  handler: {{kind: instruction}}
  workflow:
    steps:
{steps}
---
# Workflow test
"""


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
    ],
)
def test_workflow_steps_refuse_uncheckable_or_unresolvable_metadata(steps, message):
    result = validate_skill_text(_skill(steps))

    assert not result.ok
    assert any(message in error for error in result.errors)


def test_workflow_steps_accept_required_checks_and_known_dependencies():
    result = validate_skill_text(
        _skill(
            "      - id: product\n"
            "        required: true\n"
            "        check: Approved behavior is recorded\n"
            "      - id: build\n"
            "        after: [product]\n"
        )
    )

    assert result.ok


def _decision_brief(form: str):
    """The packaged decision-brief as shipped, or rewritten into the sidecar form."""
    skill_path = system_package_dir() / "skills/decision-brief/SKILL.md"
    shipped = validate_skill_file(skill_path)
    if form == "packaged":
        return shipped
    assert form == "sidecar"
    skill_md, sidecar = to_sidecar_form(skill_path.read_text(encoding="utf-8"), shipped.corsia)
    return validate_skill_bundle(skill_md, {"corsia.yaml": sidecar})


@pytest.mark.parametrize("form", ["packaged", "sidecar"])
def test_packaged_declared_workflow_contract_survives_system_cut(form):
    package = system_package_dir()
    decision = _decision_brief(form)
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
        "skipped: <step id>, <reason>",
        "workflow_ref",
        "workflow_step",
        "+<short-name>",
        "difficulty_rerated",
    ):
        assert contract in task.body
