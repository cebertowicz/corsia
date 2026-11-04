"""Bring an earlier install's content over through the writers that already own it.

A dry run reports and writes nothing; ``apply=True`` runs the same walk and
hands each item to its existing owner — ``initialize_project`` for a project,
the guidance save for context, the skill install for skills, and the durable
shared write for shared documents, so each lands with its history, its owner
and its index row. Nothing of yours is ever overwritten: a project whose name is
taken is skipped whole, and a file already at its destination is skipped and
named. The one exception is a project this run just created, where every file
present is scaffold seed and the earlier install's own version replaces it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from onboarding.earlier_install import WORKSPACE, File, Plan, Scope, Skill

NEW, ADDED, EXISTS, SKIPPED, FAILED = "new", "added", "exists", "skipped", "failed"

#: An imported shared document is its new owner's to review; it carries no trigger of its own.
UPDATE_TRIGGER = "when its owner next reviews what came over from the earlier install"
REASON = "brought over from an earlier CORSIA install"
TAKEN = "a project of that name is already here"
_THERE = "a file is already there"
_SKILL_THERE = "a skill of that name is already here"


@dataclass(frozen=True)
class Landing:
    """Where one scope's items land, and whether this run actually writes."""

    root: Path | None
    workspace: Path
    project_id: str | None
    conductor: str | None
    author: str
    db: Any
    apply: bool
    skipped: str = ""
    #: True only for a project this run created, whose files are all scaffold seed.
    created: bool = False

    @property
    def writing(self) -> bool:
        """True when this run actually writes this scope's items."""
        return self.apply and not self.skipped and self.root is not None

    def destination(self, relative: str) -> Path:
        """Where one item of this scope lands, from its path in the earlier install."""
        assert self.root is not None
        return self.root / relative

    def shared_root(self) -> Path:
        """This scope's ``shared/``, made if the scope never had one.

        A category is created with its own INDEX.md, but only inside a
        ``shared/`` that is already there, and a workspace that has never held
        a shared document has no such directory yet.
        """
        assert self.root is not None
        root = self.root / "shared"
        root.mkdir(parents=True, exist_ok=True)
        return root


def _describe(data: bytes) -> str:
    """A shared document's description: its first heading or line, as shown text."""
    from onboarding.agent_import_skills import shown_description

    for line in data.decode("utf-8", "replace").splitlines():
        if line.strip():
            return shown_description(line.lstrip("# ")) or REASON
    return REASON


def _shared_order(file: File) -> tuple:
    """Index files before the documents they index, so each category exists first."""
    parts = file.relative.split("/")
    return (
        0 if file.relative == "shared/INDEX.md" else 1,
        parts[1] if len(parts) > 2 else "",
        0 if parts[-1] == "INDEX.md" else 1,
        file.relative,
    )


def _save_context(path: Path, data: bytes, land: Landing) -> tuple[str, str]:
    """Save one context file through guidance history, keeping its compare-and-swap."""
    from infra.guidance_git import save_guidance
    from infra.guidance_history_paths import digest
    from infra.guidance_targets import guidance_target

    target = guidance_target(path, land.workspace)
    if target is None:
        return SKIPPED, "not a context file this version loads"
    current = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
    saved = save_guidance(
        {path: data}, workspace=land.workspace, target=target, author=land.author,
        reason=REASON, expected={path: digest(current)},
    )
    if saved.outcome in ("written", "unchanged", "history-off"):
        return ADDED, saved.outcome
    return (EXISTS if saved.outcome == "stale" else FAILED), saved.outcome


def _install_skill(skill: Skill, source: str, land: Landing) -> tuple[str, str]:
    """Install one skill bundle through the existing skill install."""
    from infra.skill_install import install_skill_bundle, origin_metadata

    other = {name: data for name, data in skill.files.items() if name != "SKILL.md"}
    result = install_skill_bundle(
        land.workspace, skill.name, skill.files["SKILL.md"], other, skill.description,
        origin_metadata(source, skill.files["SKILL.md"], _adopted_at()),
        author_thread_id=land.conductor or "", scope=_skill_scope(land),
        project_id=land.project_id, db=land.db, reason=REASON, binary_safe_history=True,
    )
    outcome = result.get("outcome", FAILED)
    detail = result.get("reason") or result.get("history_outcome") or ""
    if outcome == "installed":
        return ADDED, detail
    return (EXISTS, _SKILL_THERE) if outcome == "already_here" else (FAILED, detail)


def _write_shared(path: Path, data: bytes, land: Landing) -> tuple[str, str]:
    """Publish one shared document through the durable shared write."""
    from registry.durable_writes import write_text
    from registry.shared_write_errors import DurableWritePolicyError

    if path.is_symlink() or (path.exists() and not land.created):
        return EXISTS, _THERE
    root = land.shared_root()
    # A category is made by its own INDEX.md; a folder inside one is only a folder.
    if (root / path.relative_to(root).parts[0]).is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        written = write_text(
            path, data.decode("utf-8"), thread_id=land.conductor or "",
            project_id=land.project_id, workspace=land.workspace,
            description=_describe(data), owner="conductor", update_trigger=UPDATE_TRIGGER,
        )
    except (DurableWritePolicyError, OSError, ValueError) as error:
        return FAILED, str(error)
    return (ADDED, "") if isinstance(written, dict) else (FAILED, "not a shared document")


def _adopted_at() -> str:
    """When this install adopted the bundle, as the sidecar records it."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _skill_scope(land: Landing) -> str:
    return "project" if land.project_id else "workspace"


def _planned_file(relative: str, land: Landing) -> tuple[str, str]:
    """A dry run's state for one file: whatever is already at its destination wins."""
    if land.root is None:
        return NEW, ""
    path = land.destination(relative)
    return (EXISTS, _THERE) if path.exists() or path.is_symlink() else (NEW, "")


def _planned_skill(skill: Skill, land: Landing) -> tuple[str, str]:
    """A dry run's state for one skill: an installed name of its own is never replaced."""
    from core.skill_resolution import skill_scope_root

    if land.root is None:
        return NEW, ""
    root = skill_scope_root(
        scope=_skill_scope(land), workspace=land.workspace, db=land.db, project_id=land.project_id,
    )
    if root is not None and (root / "skills" / skill.name).exists():
        return EXISTS, _SKILL_THERE
    return NEW, ""


def _items(scope: Scope, land: Landing) -> dict:
    """One scope's three lists, each item carrying the state this run gave it."""
    context = [
        {"path": file.relative, **_row(
            lambda: _save_context(land.destination(file.relative), file.data, land),
            lambda: _planned_file(file.relative, land), land,
        )}
        for file in scope.context
    ]
    skills = [
        {"name": skill.name, **_row(
            lambda: _install_skill(skill, f"{scope.label}/skills/{skill.name}", land),
            lambda: _planned_skill(skill, land), land,
        )}
        for skill in scope.skills
    ]
    shared = [
        {"path": file.relative, **_row(
            lambda: _write_shared(land.destination(file.relative), file.data, land),
            lambda: _planned_file(file.relative, land), land,
        )}
        for file in sorted(scope.shared, key=_shared_order)
    ]
    return {"context": context, "skills": skills, "shared": shared}


def _row(write, plan, land: Landing) -> dict:
    """One item's state: skipped whole, written, or only reported."""
    if land.skipped:
        state, detail = SKIPPED, land.skipped
    else:
        state, detail = write() if land.writing else plan()
    return {"state": state, "detail": detail}


def _create(scope: Scope, land: Landing, user_id: str) -> tuple[str, str, Landing]:
    """Create the project and return where its items land."""
    from api.project_scaffolding import initialize_project
    from core.project_context import resolve_project_paths
    from infra.conductor import get_project_conductor, human_conductor_model

    try:
        project_id = str(initialize_project(
            land.db, name=scope.name, user_id=user_id,
            conductor_model=human_conductor_model(land.db),
        )["id"])
    except Exception as error:  # noqa: BLE001 — a creation fault is this project's result, not the run's
        return FAILED, str(error), land
    root = resolve_project_paths(land.workspace, project_id, db=land.db)[1]
    return ADDED, "", Landing(
        root=root, workspace=land.workspace, project_id=project_id,
        conductor=get_project_conductor(project_id, db=land.db), author=land.author,
        db=land.db, apply=land.apply, created=True,
    )


def _project(scope: Scope, land: Landing, user_id: str) -> dict:
    """One project, created whole or skipped whole."""
    taken = land.db.execute("SELECT id FROM projects WHERE name = ?", (scope.name,)).fetchone()
    state, detail = (EXISTS, TAKEN) if taken else (NEW, "")
    if land.apply and not taken:
        state, detail, land = _create(scope, land, user_id)
    if detail:
        land = Landing(
            root=land.root, workspace=land.workspace, project_id=land.project_id,
            conductor=land.conductor, author=land.author, db=land.db,
            apply=land.apply, skipped=detail, created=land.created,
        )
    return {
        "folder": scope.label, "name": scope.name, "state": state, "reason": detail,
        **_items(scope, land),
    }


def run(plan: Plan, *, workspace: Path, db, user_id: str, apply: bool) -> dict:
    """Report what comes over, and bring it over when ``apply`` is set."""
    from infra.corsia_agent import get_corsia_agent
    from infra.guidance_messages import saver_author

    author = saver_author(user_id=user_id)
    empty = Landing(None, workspace, None, None, author, db, apply)
    projects = [_project(scope, empty, user_id) for scope in plan.projects]
    here = Landing(workspace, workspace, None, get_corsia_agent(db=db), author, db, apply)
    items = _items(plan.workspace, here)
    return {
        "source": str(plan.source),
        "applied": apply,
        "projects": projects,
        "workspace": {"skills": items["skills"], "shared": items["shared"]},
        "left_behind": [
            {"where": entry.where or WORKSPACE, "path": entry.path, "reason": entry.reason}
            for entry in plan.left_behind
        ],
    }
