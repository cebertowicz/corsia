"""Scoped skills registry API."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from core.skills_registry import (
    SkillScope,
    local_skill_inventory,
    normalize_skill_scope,
    skill_scope_root,
)
from core.skill_validation import skill_template, validate_skill_dir_name
from infra.system_package import SYSTEM_VERSION
from infra import config
from infra.auth import authenticate, owns_thread
from infra.database import get_db
from infra.db_async import run_in_db
from infra.queries import get_thread_project_id

router = APIRouter(prefix="/api/skills", tags=["skills"])


class CreateSkillBody(BaseModel):
    scope: SkillScope
    name: str
    description: str
    project_id: str | None = None
    thread_id: str | None = None

    @field_validator("scope", mode="before")
    @classmethod
    def _normalize_scope(cls, value: str) -> SkillScope:
        if value == "system":
            # System skills ship with CORSIA and are read in place from the
            # package (#939): creating one is a CORSIA PR, not an API call.
            raise ValueError(
                "System skills ship with CORSIA; change them with a CORSIA PR"
            )
        return normalize_skill_scope(value)


def _rows(rows) -> list[dict]:
    return [row.to_dict() for row in rows]


def _project_exists(project_id: str | None, db) -> bool:
    if not project_id:
        return False
    return (
        db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
        is not None
    )


def _authorized_scope(
    *,
    scope: SkillScope,
    user_id: str,
    db,
    project_id: str | None = None,
    thread_id: str | None = None,
) -> str | None:
    """Return the project id authorized for this skill scope, or raise."""
    if scope == "thread":
        if not thread_id or not owns_thread(thread_id, user_id, db=db):
            raise HTTPException(404, "Thread not found")
        return project_id or get_thread_project_id(thread_id, db=db)
    if thread_id and not owns_thread(thread_id, user_id, db=db):
        raise HTTPException(404, "Thread not found")
    resolved_project_id = project_id or (
        get_thread_project_id(thread_id, db=db) if thread_id else None
    )
    if scope == "project" and not _project_exists(resolved_project_id, db):
        raise HTTPException(404, "Scope not found")
    # Workspace/global and project skills are shared settings surfaces today;
    # authentication is the authority until the project model grows ownership.
    return resolved_project_id


@router.get("")
async def list_skills(
    token: str,
    scope: str,
    project_id: str | None = None,
    thread_id: str | None = None,
):
    """Return skill inventory physically present in one scope."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    try:
        normalized_scope = normalize_skill_scope(scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    def _list():
        db = get_db()
        try:
            resolved_project_id = _authorized_scope(
                scope=normalized_scope,
                user_id=user["id"],
                db=db,
                project_id=project_id,
                thread_id=thread_id,
            )
            root = skill_scope_root(
                scope=normalized_scope,
                workspace=config.WORKSPACE,
                db=db,
                project_id=resolved_project_id,
                thread_id=thread_id,
            )
            if root is None:
                raise HTTPException(404, "Scope not found")
            rows = local_skill_inventory(
                scope=normalized_scope,
                workspace=config.WORKSPACE,
                db=db,
                project_id=resolved_project_id,
                thread_id=thread_id,
            )
            return {
                "scope": normalized_scope,
                "scope_id": (
                    SYSTEM_VERSION
                    if normalized_scope == "system"
                    else thread_id or resolved_project_id or "workspace"
                ),
                "root": str(root),
                "skills": _rows(rows),
            }
        finally:
            db.close()

    return await run_in_db(_list)


@router.post("")
async def create_skill(token: str, body: CreateSkillBody):
    """Create a standards-compatible ``skills/<name>/SKILL.md`` document."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _create():
        db = get_db()
        try:
            resolved_project_id = _authorized_scope(
                scope=body.scope,
                user_id=user["id"],
                db=db,
                project_id=body.project_id,
                thread_id=body.thread_id,
            )
            root = skill_scope_root(
                scope=body.scope,
                workspace=config.WORKSPACE,
                db=db,
                project_id=resolved_project_id,
                thread_id=body.thread_id,
            )
            if root is None:
                raise HTTPException(404, "Scope not found")
            name = validate_skill_dir_name(body.name)
            content = skill_template(name, body.description)
            local = local_skill_inventory(
                scope=body.scope,
                workspace=config.WORKSPACE,
                db=db,
                project_id=resolved_project_id,
                thread_id=body.thread_id,
            )
            if any(row.name == name and row.status != "error" for row in local):
                raise HTTPException(409, f"Skill already exists in this scope: {name}")
            path = root / "skills" / name / "SKILL.md"
            if path.exists():
                raise HTTPException(409, f"Skill already exists in this scope: {name}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            rows = local_skill_inventory(
                scope=body.scope,
                workspace=config.WORKSPACE,
                db=db,
                project_id=resolved_project_id,
                thread_id=body.thread_id,
            )
            row = next((row for row in rows if Path(row.path) == path.resolve(strict=False)), None)
            if row is None:
                raise HTTPException(500, "Created skill could not be loaded")
            return {"skill": row.to_dict()}
        finally:
            db.close()

    return await run_in_db(_create)
