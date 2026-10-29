"""Workflow prompts resolved outside the Plans write boundary."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from core.skill_sidecar import SIDECAR_FILE
from core.skill_validation import declares_sidecar
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
    """The prompt-relevant subset of one effective workflow skill."""

    required_steps: tuple[str, ...]
    version: str | None = None
    skill: Any = None
    #: Step id -> the workflow its ``workflow:`` key names; that step runs as a child phase.
    nested: dict[str, str] = field(default_factory=dict)


_PROMPTING_OPERATIONS = frozenset({"add", "insert", "accept", "accept_with_follow_up", "fail", "close_node"})


async def operation_workflows(actor, operation: str, params: dict[str, Any]) -> dict[str, Workflow]:
    """The workflows a plan mutation prompts from; a creation's referenced version is read off the DB."""

    if operation not in _PROMPTING_OPERATIONS:
        return {}

    def resolve() -> dict[str, Workflow]:
        db = get_db()
        try:
            return effective_workflows(actor, db)
        finally:
            db.close()

    workflows = await run_in_db(resolve)
    if operation in {"add", "insert"}:
        workflows = await run_off_db(with_referenced_version, params, workflows)
    return workflows


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
        required = tuple(
            step["id"]
            for step in steps
            if isinstance(step, dict)
            and step.get("required") is True
            and isinstance(step.get("id"), str)
        )
        nested = {
            step["id"]: step["workflow"]
            for step in steps
            if isinstance(step, dict)
            and isinstance(step.get("id"), str)
            and isinstance(step.get("workflow"), str)
        }
        workflows[skill.name] = Workflow(required_steps=required, skill=skill, nested=nested)
    return workflows


def with_referenced_version(
    params: dict[str, Any], workflows: dict[str, Workflow],
) -> dict[str, Workflow]:
    """Blocking. Resolve the version of only the workflow ``params`` references.

    Run it off the database connection: an editable skill's version costs Git
    subprocesses. A creation without a known ``workflow_ref`` runs no Git.
    """

    reference = params.get("workflow_ref")
    name = reference.strip() if isinstance(reference, str) else ""
    workflow = workflows.get(name)
    if workflow is None or workflow.skill is None:
        return workflows
    return {**workflows, name: replace(workflow, version=_workflow_version(workflow.skill))}


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


def append_add_notice(result: dict, db, workflows: dict[str, Workflow]) -> None:
    """Prompt, without blocking, for required workflow steps absent from a run."""

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
    phase = _phase_for_node(db, str(node["id"]))
    missing = _missing_required_steps(db, phase, workflows)
    if missing is None:
        _append_notice(result, _unavailable_workflow_notice(phase))
    elif missing:
        _append_notice(
            result, "Required steps not in this run: " + ", ".join(missing) + "."
        )


def append_close_notice(
    result: dict,
    *,
    node_id: str,
    db,
    workflows: dict[str, Workflow],
) -> None:
    """Prompt when a run's final task closes before a required step exists.

    A nested phase that closes ends its own run and may end its parent's, so
    both runs are checked, each line naming its workflow.
    """

    node = db.execute("SELECT kind,parent_id FROM plan_nodes WHERE id=?", (node_id,)).fetchone()
    nested = node is not None and node["kind"] == "phase" and node["parent_id"] is not None
    phases = [_phase_for_node(db, node_id)]
    if nested:
        phases.append(_phase_for_node(db, str(node["parent_id"])))
    notices = []
    for phase in phases:
        if phase is None or not _run_steps_are_terminal(db, str(phase["id"])):
            continue
        missing = _missing_required_steps(db, phase, workflows)
        run = f"this {phase['workflow_ref']} run" if nested else "this run"
        if missing is None:
            notices.append(_unavailable_workflow_notice(phase))
        else:
            notices.extend(f"Required step {step} has no task in {run}." for step in missing)
    if notices:
        result["notice"] = " ".join(notices)


def _phase_for_node(db, node_id: str):
    node = db.execute(
        "SELECT id,kind,parent_id,workflow_ref FROM plan_nodes WHERE id=?", (node_id,)
    ).fetchone()
    if node is None:
        return None
    if node["kind"] == "phase":
        return node
    if node["parent_id"] is None:
        return None
    phase = db.execute(
        "SELECT id,kind,parent_id,workflow_ref FROM plan_nodes WHERE id=?",
        (node["parent_id"],),
    ).fetchone()
    return phase if phase is not None and phase["kind"] == "phase" else None


def _missing_required_steps(
    db, phase, workflows: dict[str, Workflow],
) -> tuple[str, ...] | None:
    """Return missing steps from the currently delivered skill, if available.

    These notices follow the currently delivered skill; recorded workflow fields
    govern task and phase reporting.
    """

    if phase is None or not isinstance(phase["workflow_ref"], str):
        return ()
    workflow = workflows.get(phase["workflow_ref"])
    if workflow is None:
        return None
    # A step is planned by a task of this run, or, for a step that names a
    # workflow, by a child phase running exactly that workflow.
    planned = {
        row["workflow_step"]
        for row in db.execute(
            "SELECT kind,workflow_ref,workflow_step FROM plan_nodes WHERE parent_id=? "
            "AND archived_at IS NULL AND kind IN ('task','phase')",
            (phase["id"],),
        ).fetchall()
        if isinstance(row["workflow_step"], str)
        and isinstance(row["workflow_ref"], str)
        and row["workflow_ref"] == (
            phase["workflow_ref"] if row["kind"] == "task"
            else workflow.nested.get(row["workflow_step"])
        )
    }
    return tuple(step for step in workflow.required_steps if step not in planned)


def _unavailable_workflow_notice(phase) -> str:
    return (
        f"Workflow {phase['workflow_ref']} is no longer available; "
        "required steps not checked."
    )


def _run_steps_are_terminal(db, phase_id: str) -> bool:
    """Whether every step of a run has finished: its tasks and its nested phases."""
    rows = db.execute(
        "SELECT state,archived_at FROM plan_nodes WHERE parent_id=? AND kind IN ('task','phase')",
        (phase_id,),
    ).fetchall()
    return bool(rows) and all(
        row["archived_at"] is not None
        or row["state"] in {"accepted", "accepted_with_followup", "failed", "closed"}
        for row in rows
    )


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
