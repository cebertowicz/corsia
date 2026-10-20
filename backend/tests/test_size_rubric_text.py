"""One size rubric, quoted at every place an agent meets it, pinned to the classes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.size_rating import SIZE_CLASSES, SIZE_MEANING, SIZE_SCALE_DESCRIPTION, SIZE_TOOL_PREFIX
from plans import service
from plans.size_fields import TASK_SIZE_REQUIRED, require_task_size, validate_size
from tools.plan_field_schemas import MUTATION_FIELD_SCHEMAS

_SKILL = Path(__file__).resolve().parents[1] / "prompts/system/v1/skills/task-discipline/SKILL.md"


def test_the_rubric_names_every_size_class_in_order():
    named = re.findall(r"\b(XXL|XL|XS|S|M|L|unknown)\b", SIZE_SCALE_DESCRIPTION)
    assert tuple(named) == SIZE_CLASSES


def test_the_tool_field_carries_the_scale_with_a_short_task_requirement():
    description = MUTATION_FIELD_SCHEMAS["size"]["description"]
    assert description == (
        f"{SIZE_TOOL_PREFIX}{SIZE_SCALE_DESCRIPTION}. Every task: required; "
        "unknown if unsure."
    )
    # The field ships in the plan_update schema on every Turn: the long meaning
    # stays out of it, and the prefix is no longer than the one it replaced.
    assert SIZE_MEANING not in description
    assert len(SIZE_TOOL_PREFIX.encode()) <= len("Human-equivalent effort to deliver the node: ".encode())


def test_the_skill_states_the_exact_rubric_once_in_its_general_plan_guidance():
    text = _SKILL.read_text(encoding="utf-8")
    assert text.count(SIZE_SCALE_DESCRIPTION) == 1
    plan_section = text.split("## Plan", 1)[1].split("## Declared workflows", 1)[0]
    assert (
        f"- **Size.** {SIZE_MEANING} {SIZE_SCALE_DESCRIPTION}. Set `size` on every task you add or insert."
        in plan_section
    )
    workflow = text.split("## Declared workflows", 1)[1].split("\n## ", 1)[0]
    assert "under 15 min" not in workflow and "(see **Size**)" in workflow


def test_the_refusals_quote_the_same_rubric():
    with pytest.raises(service.PlanInputError) as invalid:
        validate_size("huge")
    assert SIZE_MEANING in str(invalid.value)
    assert SIZE_SCALE_DESCRIPTION in str(invalid.value)

    with pytest.raises(service.PlanInputError) as missing:
        require_task_size(None)
    assert str(missing.value) == TASK_SIZE_REQUIRED
    assert "unknown" in str(missing.value)
