"""Workflow prompts resolved outside the Plans write boundary."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.skill_sidecar import SIDECAR_FILE
from core.skill_validation import declares_sidecar, instruction_section, parse_frontmatter
from core.skills_registry import resolve_effective_skills
from infra import config
from infra.database import get_db
from infra.db_async import run_in_db, run_off_db
from infra.guidance_git import GitFault, file_version, workspace_repository
from infra.guidance_history_paths import LOCAL_EDIT_MARK
from infra.system_package import system_package_dir
from plans.public import PlanInputError


@dataclass(frozen=True)
class Workflow:
    """The plan-relevant subset of one effective workflow skill."""

    #: The definition's steps, as written; a ``workflow:`` key makes a step a child phase.
    steps: tuple[dict[str, Any], ...] = ()
    version: str | None = None
    skill: Any = None
    #: The plain steps a phase add builds its run from (:func:`with_referenced_version`).
    run: tuple[dict[str, Any], ...] = ()


async def operation_workflows(actor, operation: str, params: dict[str, Any]) -> dict[str, Workflow]:
    """The workflows a creation reads; its referenced version and run are read off the DB."""

    if operation not in {"add", "insert"}:
        return {}

    def resolve() -> dict[str, Workflow]:
        db = get_db()
        try:
            return effective_workflows(actor, db)
        finally:
            db.close()

    workflows = await run_in_db(resolve)
    return await run_off_db(with_referenced_version, params, workflows)


def effective_workflows(actor, db) -> dict[str, Workflow]:
    """Resolve effective skills, including the System tier, before a plan write.

    Versions stay unresolved: :func:`with_referenced_version` reads the one a
    creation names, off the database connection.
    """

    if actor.thread_id is None:
        return {}
    resolution = resolve_effective_skills(
        thread_id=actor.thread_id,
        project_id=actor.project_id,
        workspace=config.WORKSPACE,
        db=db,
    )
    workflows: dict[str, Workflow] = {}
    for skill in resolution.included:
        workflow = skill.corsia.get("workflow") if isinstance(skill.corsia, dict) else None
        steps = workflow.get("steps") if isinstance(workflow, dict) else None
        if not isinstance(steps, list):
            continue
        steps = tuple(step for step in steps if isinstance(step, dict) and isinstance(step.get("id"), str))
        workflows[skill.name] = Workflow(steps=steps, skill=skill)
    return workflows


def with_referenced_version(
    params: dict[str, Any], workflows: dict[str, Workflow],
) -> dict[str, Workflow]:
    """Blocking. Resolve the version of only the workflow ``params`` references, and a phase's run.

    Run it off the database connection: an editable skill's version costs Git
    subprocesses, and a step's instruction is read from its SKILL.md. A creation
    without a known ``workflow_ref`` runs no Git.

    Raises:
        PlanInputError: A step names a workflow that isn't available or already runs
            above it, or an instruction heading its SKILL.md doesn't have.
    """

    reference = params.get("workflow_ref")
    name = reference.strip() if isinstance(reference, str) else ""
    workflow = workflows.get(name)
    if workflow is None or workflow.skill is None:
        return workflows
    run = _run(name, workflows, (name,)) if params.get("kind") == "phase" else ()
    return {**workflows, name: replace(workflow, version=_workflow_version(workflow.skill), run=run)}


def _run(name: str, workflows: dict[str, Workflow], trail: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
    """Blocking. The plain steps a phase running ``name`` is built from, nested runs included."""

    workflow = workflows[name]
    body: str | None = None
    run = []
    for step in workflow.steps:
        step_id = step["id"]
        difficulty = step.get("difficulty")
        built: dict[str, Any] = {
            "id": step_id, "title": step.get("title"), "check": step.get("check"),
            "required": step.get("required") is True,
            "after": [value.strip() for value in step.get("after") or () if isinstance(value, str)],
            "difficulty": difficulty.get("default") if isinstance(difficulty, dict) else None,
        }
        heading = step.get("instruction")
        if isinstance(heading, str):
            if body is None:
                body = _skill_body(name, step_id, workflow.skill)
            built["instruction"] = instruction_section(body, heading)
            if built["instruction"] is None:
                raise PlanInputError(
                    f"step {step_id} names instruction {heading.strip()!r}, which {name}'s SKILL.md doesn't have"
                )
        nested = step.get("workflow")
        if isinstance(nested, str):
            if nested in trail:
                raise PlanInputError(
                    f"step {step_id} names workflow {nested}, which already runs above it: "
                    + " -> ".join((*trail, nested))
                )
            if nested not in workflows:
                raise PlanInputError(f"step {step_id} names workflow {nested}, which isn't available here")
            inner = workflows[nested].skill
            built.update(
                workflow=nested, version=None if inner is None else _workflow_version(inner),
                steps=_run(nested, workflows, (*trail, nested)),
            )
        run.append(built)
    return tuple(run)


def _skill_body(name: str, step_id: str, skill) -> str:
    """The SKILL.md body (frontmatter removed) a step's instruction is copied from."""
    try:
        return parse_frontmatter(Path(skill.body_path).read_text(encoding="utf-8"))[1]
    except (OSError, ValueError) as exc:
        raise PlanInputError(f"step {step_id}'s instruction can't be read from {name}'s SKILL.md") from exc


def validate_creation_reference(params: dict[str, Any], workflows: dict[str, Workflow]) -> None:
    """Require a supplied workflow reference to name an effective workflow skill."""

    reference = params.get("workflow_ref")
    if reference is None or not isinstance(reference, str):
        return
    if reference.strip() not in workflows:
        available = ", ".join(sorted(workflows)) or "none; omit workflow_ref"
        raise PlanInputError(
            f"workflow_ref {reference!r} is not an effective workflow skill; "
            f"effective workflow refs: {available}"
        )


def creation_version(params: dict[str, Any], workflows: dict[str, Workflow]) -> str | None:
    """Resolve the persisted workflow version from the named effective skill."""

    reference = params.get("workflow_ref")
    if not isinstance(reference, str):
        return None
    workflow = workflows.get(reference.strip())
    return None if workflow is None else workflow.version


def creation_run(params: dict[str, Any], workflows: dict[str, Workflow]) -> tuple[dict[str, Any], ...]:
    """The plain steps a phase add builds under itself; empty for anything else."""

    reference = params.get("workflow_ref")
    workflow = workflows.get(reference.strip()) if isinstance(reference, str) else None
    return () if workflow is None else workflow.run


def append_add_notice(result: dict, db, workflows: dict[str, Workflow]) -> None:
    """Say a creation's workflow version is unknown, or that its workflow already runs above it."""

    node = result.get("node")
    if not isinstance(node, dict):
        return
    reference = node.get("workflow_ref")
    workflow = workflows.get(reference) if isinstance(reference, str) else None
    if workflow is not None and workflow.version is None:
        _append_notice(result, "Workflow version unavailable; recorded null.")
    if node.get("kind") == "phase" and isinstance(reference, str):
        parent_id = node.get("parent_id")
        while parent_id is not None:
            parent = db.execute(
                "SELECT id,kind,parent_id,workflow_ref FROM plan_nodes WHERE id=?", (parent_id,),
            ).fetchone()
            if parent is None:
                break
            if parent["kind"] == "phase" and parent["workflow_ref"] == reference:
                _append_notice(
                    result,
                    f"Workflow {reference} is already running above this phase, so its steps "
                    "will be planned twice. If you didn't mean to nest it, use the outer run.",
                )
                break
            parent_id = parent["parent_id"]


def _workflow_version(skill) -> str | None:
    """Return the immutable version for one effective workflow skill."""

    return (
        _system_workflow_version(skill)
        if skill.scope == "system"
        else _editable_workflow_version(skill.path)
    )


def judged_version(version: object) -> bool:
    """Whether a recorded ``workflow_version`` names one exact content: not Unversioned, not a local edit.

    A local edit carries the mark :func:`_editable_workflow_version` writes, and two drafts can share it.
    """
    return isinstance(version, str) and bool(version.strip()) and LOCAL_EDIT_MARK not in version


def version_short_hash(version: str) -> str:
    """A recorded version's content hash cut to 7 characters, keeping the local-edit mark when it has one."""
    content = version.replace(LOCAL_EDIT_MARK, "")
    short = content.rsplit(":", 1)[-1][:7]
    return short if content == version else short + LOCAL_EDIT_MARK


def _editable_workflow_version(path) -> str | None:
    """Return the last Git commit for one editable skill, marking local edits.

    A skill whose frontmatter points at its ``corsia.yaml`` carries both files' versions,
    so a step edit changes it; a stray ``corsia.yaml`` nothing points at does not. Any Git
    fault records null (never a clean SHA), through the guidance runner.
    """

    repo, _why = workspace_repository(Path(config.WORKSPACE))
    if repo is None:
        return None
    sidecar = Path(path).parent / SIDECAR_FILE
    try:
        version = file_version(repo, Path(path))
        if version is None or not _points_at_sidecar(Path(path), sidecar):
            return version
        sidecar_version = file_version(repo, sidecar)
    except (GitFault, ValueError):
        return None
    return None if sidecar_version is None else f"{version}+{sidecar_version}"


def _points_at_sidecar(skill_md: Path, sidecar: Path) -> bool:
    """True when ``SKILL.md`` declares a ``corsia.yaml`` pointer and the file is there."""
    try:
        return sidecar.is_file() and declares_sidecar(skill_md.read_text(encoding="utf-8"))
    except OSError:
        return False


def _system_workflow_version(skill) -> str | None:
    """Return the System source id plus manifest-recorded raw file digest.

    A System skill whose frontmatter points at its ``corsia.yaml`` appends the sidecar's
    manifest digest when the manifest lists it.
    """

    from core.system_tier import system_tier_manifest

    relative = f"skills/{skill.name}/SKILL.md"
    sidecar = f"skills/{skill.name}/{SIDECAR_FILE}"
    if not (system_package_dir() / relative).is_file():
        return None
    try:
        manifest = system_tier_manifest()
        declared = declares_sidecar((system_package_dir() / relative).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    wanted = [relative]
    if declared and any(item.path == sidecar for item in manifest.files):
        wanted.append(sidecar)
    digests = []
    for item_path in wanted:
        entry = next((item for item in manifest.files if item.path == item_path), None)
        if entry is None or any(item.path == item_path for item in manifest.mismatches):
            return None
        digests.append(entry.sha256)
    return f"system:{relative}:{manifest.version}:{'+'.join(digests)}"


def _append_notice(result: dict, notice: str) -> None:
    """Add one visible line without discarding another creation notice."""

    existing = result.get("notice")
    result["notice"] = f"{existing}\n{notice}" if existing else notice
