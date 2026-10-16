"""The agent-setup steps in coordination-api name what the product enforces."""
from __future__ import annotations

from pathlib import Path

SKILL = (
    Path(__file__).resolve().parents[1]
    / "prompts/system/v1/skills/coordination-api/SKILL.md"
).read_text()


def _step(number: int) -> str:
    section = SKILL.split("## Create or promote an agent", 1)[1]
    return next(line for line in section.splitlines() if line.startswith(f"{number}. "))


def test_rename_field_is_named_on_the_roster_step():
    from tools.agent_roster_update import AgentRosterUpdateTool

    assert "name" in AgentRosterUpdateTool().parameters["properties"]
    assert "(and `name` to rename it)" in _step(3)


def test_home_step_writes_the_index_then_rehomes_it():
    step = _step(5)
    assert "shared/<domain>/INDEX.md" in step
    assert "reuse it and leave that index as it is" in step
    assert "add the category's line to `shared/INDEX.md`" in step
    assert "`artifact_rehome`" in step and "`artifact_resolve`" in step
    assert "refused until that file exists" in step
    assert "can't edit an index someone else owns" in step


def test_final_step_checks_the_agent_is_visible_in_the_agents_list():
    step = _step(7)
    assert "`GET /api/agents` lists it as a visible thread (not a subagent)" in step
