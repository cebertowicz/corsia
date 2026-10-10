"""One size rubric, quoted at every place an agent meets it, pinned to the classes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.size_rating import SIZE_CLASSES, SIZE_SCALE_DESCRIPTION
from plans import service
from plans.size_fields import validate_size
from tools.plan_field_schemas import MUTATION_FIELD_SCHEMAS

_SKILL = Path(__file__).resolve().parents[1] / "prompts/system/v1/skills/task-discipline/SKILL.md"


def test_the_rubric_names_every_size_class_in_order():
    named = re.findall(r"\b(XXL|XL|XS|S|M|L|unknown)\b", SIZE_SCALE_DESCRIPTION)
    assert tuple(named) == SIZE_CLASSES


def test_the_tool_field_carries_the_rubric_and_tells_agents_to_set_it():
    description = MUTATION_FIELD_SCHEMAS["size"]["description"]
    assert SIZE_SCALE_DESCRIPTION in description
    assert "every task you add" in description


def test_the_skill_states_the_rubric_once_in_its_general_plan_guidance():
    text = _SKILL.read_text(encoding="utf-8")
    assert text.count(SIZE_SCALE_DESCRIPTION) == 1
    plan_section = text.split("## Plan", 1)[1].split("## Declared workflows", 1)[0]
    assert "- **Size.** Set `size` on each task node you add or insert" in plan_section
    assert SIZE_SCALE_DESCRIPTION in plan_section
    workflow = text.split("## Declared workflows", 1)[1].split("\n## ", 1)[0]
    assert "under 15 min" not in workflow and "(see **Size**)" in workflow


def test_the_refusal_quotes_the_same_rubric():
    with pytest.raises(service.PlanInputError) as error:
        validate_size("huge")
    assert SIZE_SCALE_DESCRIPTION in str(error.value)
