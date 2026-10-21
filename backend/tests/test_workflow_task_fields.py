"""Workflow task-field contracts at the plan boundary."""

from __future__ import annotations

from dataclasses import asdict
import itertools
import sqlite3
import subprocess

import pytest

from core.skills_registry import SkillResolution, SkillRow
from infra.schema import SCHEMA_SQL
from infra.system_package import system_package_dir
from plans import service
from plans.boundary import PlanActor, mutate, read_current_plan
from plans.current import read_current_status
from tests.skill_sidecar_helpers import POINTER
from tools.workflow_task_fields import (
    Workflow,
    append_add_notice,
    append_close_notice,
)


_MUTATION_IDS = itertools.count()


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) VALUES ('owner','user','t','t')"
    )
    connection.commit()
    yield connection
    connection.close()


def _actor() -> PlanActor:
    return PlanActor(thread_id="owner", user_id="user", project_id=None)


def _mutate(db, plan_id, operation, params, workflow_version=None):
    return mutate(
        plan_id,
        operation,
        params,
        actor=_actor(),
        provenance="test.workflow",
        mutation_id=f"workflow-{next(_MUTATION_IDS)}",
        db=db,
        workflow_version=workflow_version,
    ).result


def test_add_insert_immutable_fields_and_read_detail(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _mutate(
        db,
        plan.id,
        "add",
        {
            "kind": "phase", "title": "Run workflow", "acceptance_criteria": "Run recorded",
            "workflow_ref": "sample",
        },
        workflow_version="abc123",
    )["node"]
    task = _mutate(
        db,
        plan.id,
        "insert",
        {
            "kind": "task", "title": "Build", "acceptance_criteria": "Build passes",
            "parent_id": phase["id"], "workflow_ref": "sample",
            "workflow_step": "build", "difficulty": "hard", "size": "XL",
        },
        workflow_version="abc123",
    )["node"]

    assert phase["difficulty"] is None
    assert {key: task[key] for key in (
        "workflow_ref", "workflow_version", "workflow_step", "difficulty", "size",
    )} == {
        "workflow_ref": "sample", "workflow_version": "abc123",
        "workflow_step": "build", "difficulty": "hard", "size": "XL",
    }
    detail = read_current_status(actor=_actor(), node_id=task["id"], db=db)
    assert {key: detail[key] for key in (
        "workflow_ref", "workflow_version", "workflow_step", "difficulty", "size",
    )} == {key: task[key] for key in (
        "workflow_ref", "workflow_version", "workflow_step", "difficulty", "size",
    )}
    full = read_current_plan(actor=_actor(), include_folded=True, db=db)
    listed_task = next(node for node in full["nodes"] if node["id"] == task["id"])
    assert not {"workflow_ref", "workflow_version", "workflow_step", "difficulty"} & set(listed_task)
    assert listed_task["size"] == "XL"
    assert "size" not in next(node for node in full["nodes"] if node["id"] == phase["id"])
    with pytest.raises(service.PlanInputError, match="unexpected parameters for edit: workflow_ref"):
        _mutate(
            db, plan.id, "edit",
            {"node_id": task["id"], "workflow_ref": "other", "expected_revision": task["revision"]},
        )


def test_workflow_step_requires_difficulty_and_only_allows_the_three_levels(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    base = {
        "kind": "task", "title": "Build", "acceptance_criteria": "Build passes", "size": "S",
        "workflow_ref": "sample", "workflow_step": "build",
    }
    with pytest.raises(service.PlanInputError, match="is the wanted result clear"):
        _mutate(db, plan.id, "add", base)
    with pytest.raises(service.PlanInputError, match="routine, standard, hard"):
        _mutate(db, plan.id, "add", {**base, "difficulty": "impossible"})


def test_rerating_keeps_original_difficulty_and_current_attempt(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    task = _mutate(
        db,
        plan.id,
        "add",
        {
            "kind": "task", "title": "Build", "acceptance_criteria": "Build passes", "size": "S",
            "workflow_ref": "sample", "workflow_step": "build", "difficulty": "standard",
        },
    )["node"]
    active = _mutate(
        db, plan.id, "activate", {"node_id": task["id"], "expected_revision": task["revision"]},
    )["node"]
    attempts_before = db.execute(
        "SELECT count(*) FROM plan_node_attempts WHERE node_id=?", (task["id"],)
    ).fetchone()[0]
    rerated = _mutate(
        db,
        plan.id,
        "edit",
        {
            "node_id": task["id"], "expected_revision": active["revision"],
            "difficulty_rerated": {"level": "hard", "reason": "Shared migration risk"},
        },
    )["node"]

    assert rerated["difficulty"] == "standard"
    assert db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='difficulty_rerated'",
        (task["id"],),
    ).fetchone()[0] == '{"level": "hard", "reason": "Shared migration risk"}'
    assert db.execute(
        "SELECT count(*) FROM plan_node_attempts WHERE node_id=?", (task["id"],)
    ).fetchone()[0] == attempts_before


def test_size_declaration_preserves_attempt_and_requirements_revision(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    created = _mutate(
        db, plan.id, "add",
        {"kind": "task", "title": "Build", "acceptance_criteria": "Build passes", "size": "S"},
    )["node"]
    active = _mutate(
        db, plan.id, "activate", {"node_id": created["id"], "expected_revision": created["revision"]},
    )["node"]
    attempts_before = db.execute(
        "SELECT count(*) FROM plan_node_attempts WHERE node_id=?", (created["id"],)
    ).fetchone()[0]
    edited = _mutate(
        db, plan.id, "edit",
        {"node_id": created["id"], "expected_revision": active["revision"], "size": "M"},
    )["node"]

    assert edited["size"] == "M"
    assert edited["requirements_revision"] == active["requirements_revision"]
    assert db.execute(
        "SELECT count(*) FROM plan_node_attempts WHERE node_id=?", (created["id"],)
    ).fetchone()[0] == attempts_before
    assert db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='size_declared'",
        (created["id"],),
    ).fetchone()[0] == '{"from": "S", "to": "M"}'
    with pytest.raises(service.PlanInputError, match="XS under 15 min"):
        _mutate(
            db, plan.id, "edit",
            {"node_id": created["id"], "expected_revision": edited["revision"], "size": "huge"},
        )


def test_split_copies_workflow_fields_but_follow_up_does_not(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    source = _mutate(
        db,
        plan.id,
        "add",
        {
            "kind": "task", "title": "Build", "acceptance_criteria": "Build passes",
            "workflow_ref": "sample", "workflow_step": "build", "difficulty": "standard", "size": "L",
        },
        workflow_version="abc",
    )["node"]
    split = _mutate(
        db,
        plan.id,
        "split",
        {
            "node_id": source["id"], "expected_revision": source["revision"],
            "title": "Build remainder", "acceptance_criteria": "Remainder passes", "size": "S",
        },
    )["node"]
    follow_up = _mutate(
        db,
        plan.id,
        "add_follow_up",
        {
            "node_id": source["id"], "expected_revision": source["revision"] + 1,
            "title": "Later", "acceptance_criteria": "Later passes", "size": "S",
        },
    )["follow_up"]

    for key in ("workflow_ref", "workflow_version", "workflow_step", "difficulty"):
        assert split[key] == source[key]
        assert follow_up[key] is None
    assert source["size"] == "L"
    assert split["size"] == "S" and follow_up["size"] == "S"


def test_required_step_prompts_are_report_only(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Run", acceptance_criteria="Run recorded",
        workflow_ref="sample", workflow_version="abc", db=db,
    )
    task = service.add_node(
        plan.id, kind="task", title="Build", acceptance_criteria="Build passes",
        parent_id=phase.id, workflow_ref="sample", workflow_version="abc",
        workflow_step="build", difficulty="standard", db=db,
    )
    workflows = {"sample": Workflow(required_steps=("build", "review"), version="abc")}
    added = {"node": asdict(task)}
    append_add_notice(added, db, workflows)
    assert added["notice"] == "Required steps not in this run: review."

    service.transition_node(task.id, "active", db=db)
    service.transition_node(task.id, "closed", db=db)
    closed: dict = {}
    append_close_notice(closed, node_id=task.id, db=db, workflows=workflows)
    assert closed["notice"] == "Required step review has no task in this run."


@pytest.mark.parametrize("workflow_ref", ["other", None])
def test_required_step_prompts_ignore_other_or_unreferenced_workflows(db, workflow_ref):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Sample run", acceptance_criteria="Run recorded",
        workflow_ref="sample", workflow_version="abc", db=db,
    )
    service.add_node(
        plan.id, kind="task", title="Other task", acceptance_criteria="Recorded",
        parent_id=phase.id, workflow_ref=workflow_ref, workflow_step="build",
        difficulty="standard", db=db,
    )

    result: dict = {"node": asdict(phase)}
    append_add_notice(result, db, {"sample": Workflow(required_steps=("build",), version="abc")})

    assert result["notice"] == "Required steps not in this run: build."


def test_required_step_prompts_name_an_unavailable_workflow(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Sample run", acceptance_criteria="Run recorded",
        workflow_ref="sample", workflow_version="abc", db=db,
    )
    task = service.add_node(
        plan.id, kind="task", title="Task", acceptance_criteria="Recorded",
        parent_id=phase.id, db=db,
    )

    added: dict = {"node": asdict(task)}
    append_add_notice(added, db, {})
    assert added["notice"] == (
        "Workflow sample is no longer available; required steps not checked."
    )

    service.transition_node(task.id, "active", db=db)
    service.transition_node(task.id, "closed", db=db)
    closed: dict = {}
    append_close_notice(closed, node_id=task.id, db=db, workflows={})
    assert closed["notice"] == (
        "Workflow sample is no longer available; required steps not checked."
    )


def test_effective_workflows_read_the_corsia_block(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tools import workflow_task_fields

    path = tmp_path / "current" / "SKILL.md"
    steps = [{"id": "build", "required": True, "check": "Build passes"}]
    current = SkillRow(
        name="current", description="current", scope="workspace", scope_id="workspace",
        path=path, body_path=path, metadata={"corsia": {"workflow": {"steps": steps}}},
        status="included", corsia={"workflow": {"steps": steps}},
    )
    monkeypatch.setattr(
        workflow_task_fields, "resolve_effective_skills",
        lambda **_kwargs: SkillResolution([current], [], []),
    )
    actor = SimpleNamespace(thread_id="owner", project_id=None)

    workflows = workflow_task_fields.effective_workflows(actor, None)

    assert {name: flow.required_steps for name, flow in workflows.items()} == {"current": ("build",)}


@pytest.mark.asyncio
async def test_registered_plan_tools_round_trip_effective_workflow_fields(
    isolated_db, tmp_path, monkeypatch,
):
    """The real tool resolves the workflow, prompts, and returns read detail."""

    import json

    from infra.database import get_db
    from tests.event_routing_helpers import mk_session
    from tests.plan_execution_helpers import execute_as, open_turn, registry_of
    from tools.plans import PlanReadTool, PlanUpdateTool
    from tools import workflow_task_fields

    workspace = tmp_path / "workspace"
    skill = workspace / "skills" / "sample" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: sample\ndescription: Sample workflow\n{POINTER}---\n", encoding="utf-8")
    (skill.parent / "corsia.yaml").write_text(
        "handler: {kind: instruction}\nworkflow:\n  steps:\n"
        "    - {id: build, required: true, check: Build passes}\n"
        "    - {id: review, required: true, check: Review passes}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-qm", "Add sample workflow"],
        cwd=workspace, check=True,
    )
    expected_workspace_version = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    # SKILL.md and corsia.yaml were committed together: the version carries both file versions.
    expected_workspace_version = f"{expected_workspace_version}+{expected_workspace_version}"
    unversioned = workspace / "skills" / "unversioned" / "SKILL.md"
    unversioned.parent.mkdir()
    unversioned.write_text(skill.read_text(encoding="utf-8").replace("sample", "unversioned"), encoding="utf-8")
    (unversioned.parent / "corsia.yaml").write_text(
        (skill.parent / "corsia.yaml").read_text(encoding="utf-8"), encoding="utf-8",
    )
    monkeypatch.setattr(workflow_task_fields.config, "WORKSPACE", workspace)
    thread_id = mk_session("workflow-tool")
    db = get_db()
    try:
        plan = service.ensure_plan("thread", thread_id, db=db)
        identity = open_turn(db, thread_id)
    finally:
        db.close()
    registry = registry_of(PlanUpdateTool(), PlanReadTool())
    phase_result = json.loads(await execute_as(
        registry, identity, thread_id, "plan_update",
        {
            "plan_id": plan.id, "mutation_id": "phase", "operation": "add",
            "params": {
                "kind": "phase", "title": "Run sample", "acceptance_criteria": "Run recorded",
                "workflow_ref": "sample",
            },
        },
    ))
    phase = phase_result["node"]
    missing_difficulty = await execute_as(
        registry, identity, thread_id, "plan_update",
        {
            "plan_id": plan.id, "mutation_id": "missing-difficulty", "operation": "add",
            "params": {
                "kind": "task", "title": "Missing rating", "acceptance_criteria": "Never", "size": "S",
                "parent_id": phase["id"], "workflow_ref": "sample", "workflow_step": "build",
            },
        },
    )
    task = json.loads(await execute_as(
        registry, identity, thread_id, "plan_update",
        {
            "plan_id": plan.id, "mutation_id": "build", "operation": "add",
            "params": {
                "kind": "task", "title": "Build", "acceptance_criteria": "Build passes", "size": "S",
                "parent_id": phase["id"], "workflow_ref": "sample",
                "workflow_step": "build", "difficulty": "standard",
            },
        },
    ))
    detail = json.loads(await execute_as(
        registry, identity, thread_id, "plan_read", {"node_id": task["node"]["id"]},
    ))

    rejected_version = await execute_as(
        registry, identity, thread_id, "plan_update",
        {
            "plan_id": plan.id, "mutation_id": "caller-version", "operation": "add",
            "params": {
                "kind": "task", "title": "Caller version", "acceptance_criteria": "Never", "size": "S",
                "workflow_ref": "sample", "workflow_version": "invented",
            },
        },
    )
    unknown = json.loads(await execute_as(
        registry, identity, thread_id, "plan_update",
        {
            "plan_id": plan.id, "mutation_id": "unknown-version", "operation": "add",
            "params": {
                "kind": "phase", "title": "Run unversioned",
                "acceptance_criteria": "Run recorded", "workflow_ref": "unversioned",
            },
        },
    ))

    assert phase_result["node"]["workflow_version"] == expected_workspace_version
    assert phase_result["notice"] == "Required steps not in this run: build, review."
    assert "is the wanted result clear" in missing_difficulty
    assert task["node"]["workflow_version"] == expected_workspace_version
    assert task["notice"] == "Required steps not in this run: review."
    assert detail["workflow_step"] == "build"
    assert detail["difficulty"] == "standard"
    assert "workflow_version" in rejected_version and "Caller version" not in rejected_version
    assert unknown["node"]["workflow_version"] is None
    assert unknown["notice"].startswith("Workflow version unavailable; recorded null.")


@pytest.mark.asyncio
async def test_only_a_referenced_workflow_version_runs_git_and_a_fault_records_null(
    isolated_db, tmp_path, monkeypatch,
):
    """Git runs only for the workflow a creation names; a Git fault is null, never clean."""

    import json

    from infra import guidance_git
    from infra.database import get_db
    from tests.event_routing_helpers import mk_session
    from tests.plan_execution_helpers import execute_as, open_turn, registry_of
    from tools.plans import PlanUpdateTool
    from tools import workflow_task_fields

    workspace = tmp_path / "workspace"
    skill = workspace / "skills" / "sample" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: sample\ndescription: Sample workflow\n{POINTER}---\n", encoding="utf-8")
    (skill.parent / "corsia.yaml").write_text(
        "handler: {kind: instruction}\nworkflow:\n  steps:\n"
        "    - {id: build, required: true, check: Build passes}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-qm", "Add sample workflow"],
        cwd=workspace, check=True,
    )
    monkeypatch.setattr(workflow_task_fields.config, "WORKSPACE", workspace)
    real, calls = guidance_git._git, []

    def counting(repo, *args, **kwargs):
        calls.append(args[0])
        if args[0] == "status" and fail_status:
            raise guidance_git.GitFault("git status failed: corrupt index")
        return real(repo, *args, **kwargs)

    fail_status = False
    monkeypatch.setattr(guidance_git, "_git", counting)
    thread_id = mk_session("workflow-git-calls")
    db = get_db()
    try:
        plan = service.ensure_plan("thread", thread_id, db=db)
        identity = open_turn(db, thread_id)
    finally:
        db.close()
    registry = registry_of(PlanUpdateTool())

    async def add(mutation_id, **params):
        return json.loads(await execute_as(
            registry, identity, thread_id, "plan_update",
            {"plan_id": plan.id, "mutation_id": mutation_id, "operation": "add",
             "params": {"kind": "phase", "title": mutation_id, "acceptance_criteria": "Recorded", **params}},
        ))

    plain = await add("no-workflow")
    assert calls == [] and plain["node"]["workflow_version"] is None

    referenced = await add("referenced", workflow_ref="sample")
    assert {"log", "status"} <= set(calls)
    assert len(referenced["node"]["workflow_version"]) == 40 + 1 + 40

    fail_status = True
    faulted = await add("faulted", workflow_ref="sample")
    assert faulted["node"]["workflow_version"] is None
    assert faulted["notice"].startswith("Workflow version unavailable; recorded null.")


@pytest.mark.asyncio
async def test_registered_plan_tools_record_system_workflow_version(isolated_db, monkeypatch):
    """System workflows use their packaged source id and manifest digest."""

    import json

    from core.system_tier import system_tier_manifest
    from infra.database import get_db
    from tests.event_routing_helpers import mk_session
    from tests.plan_execution_helpers import execute_as, open_turn, registry_of
    from tools.plans import PlanUpdateTool
    from tools import workflow_task_fields

    skill = system_package_dir() / "skills" / "task-discipline" / "SKILL.md"
    system_skill = SkillRow(
        name="task-discipline", description="System workflow", scope="system",
        scope_id="v1", path=skill, body_path=skill,
        metadata={"corsia": {"workflow": {"steps": []}}}, status="included",
        corsia={"workflow": {"steps": []}},
    )
    monkeypatch.setattr(
        workflow_task_fields, "resolve_effective_skills",
        lambda **_kwargs: SkillResolution([system_skill], [], []),
    )
    thread_id = mk_session("system-workflow-tool")
    db = get_db()
    try:
        plan = service.ensure_plan("thread", thread_id, db=db)
        identity = open_turn(db, thread_id)
    finally:
        db.close()
    registry = registry_of(PlanUpdateTool())
    result = json.loads(await execute_as(
        registry, identity, thread_id, "plan_update",
        {
            "plan_id": plan.id, "mutation_id": "system-phase", "operation": "add",
            "params": {
                "kind": "phase", "title": "Run system",
                "acceptance_criteria": "System run recorded",
                "workflow_ref": "task-discipline",
            },
        },
    ))
    manifest = system_tier_manifest()
    entry = next(item for item in manifest.files if item.path == "skills/task-discipline/SKILL.md")

    assert result["node"]["workflow_version"] == (
        f"system:{entry.path}:{manifest.version}:{entry.sha256}"
    )
