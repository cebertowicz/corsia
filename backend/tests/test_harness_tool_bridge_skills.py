from __future__ import annotations

import sqlite3

import pytest


@pytest.mark.asyncio
async def test_harness_tool_bridge_can_load_skill_body(
    isolated_db,
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    monkeypatch.setattr("infra.config.WORKSPACE", workspace)
    import tools.sandbox_config as sandbox_config

    monkeypatch.setattr(sandbox_config, "WORKSPACE", workspace)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])

    from subagents.harness_tool_bridge import (
        build_tool_registry,
        dynamic_tool_definitions,
        execute_tool,
    )
    monkeypatch.setattr("subagents.harness_tool_bridge.WORKSPACE", workspace)

    skill_path = workspace / "skills" / "review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\n"
        "name: review\n"
        "description: Use when reviewing code.\n"
        "---\n"
        "# Review\n\n"
        "Inspect the diff and report defects first.\n",
        encoding="utf-8",
    )

    db = sqlite3.connect(isolated_db)
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,created_at,updated_at) VALUES (?,?,?,?,?)",
        ("thread-1", "steve", "test/model", "t", "t"),
    )
    db.commit()
    db.close()

    from core.turn_core import persist_turn_start
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from subagents.child_thread import persist_assistant_step

    turn_db = get_db()
    admission = admit_input(
        "thread-1",
        role="user",
        content="read the skill",
        intent="apply_now",
        message_id="skill-input",
        turn_id="skill-turn",
        boot_epoch="boot",
        admitted_at="t",
        db=turn_db,
    )
    assert admission.batch is not None
    identity = persist_turn_start(
        "skill-turn", "thread-1", "test/model", "read the skill", db=turn_db,
    )
    step_id = persist_assistant_step(
        "thread-1",
        content="",
        tool_uses=[{
            "id": "skill-call", "name": "read_file",
            "arguments": {"path": str(skill_path)},
        }],
        turn_id=identity.turn_id,
        db=turn_db,
    )
    turn_db.commit()
    turn_db.close()

    registry = build_tool_registry(str(workspace))
    tool_names = {
        definition["function"]["name"]
        for definition in dynamic_tool_definitions(registry)
        if definition.get("type") == "function"
    }
    assert "read_file" in tool_names

    result = await execute_tool(
        registry,
        thread_id="thread-1",
        turn_identity=identity,
        invocation_id="skill-invocation",
        provider_tool_call_id="skill-call",
        assistant_step_id=step_id,
        checkpoint_generation_id="installed-generation",
        name="read_file",
        arguments={"path": str(skill_path)},
    )

    assert "description: Use when reviewing code." in result
    assert "# Review" in result
    assert "Inspect the diff and report defects first." in result
    check = get_db()
    try:
        receipt = check.execute(
            "SELECT checkpoint_generation_id FROM guidance_loads WHERE path=?",
            (str(skill_path.resolve()),),
        ).fetchone()
    finally:
        check.close()
    assert receipt["checkpoint_generation_id"] == "installed-generation"
