"""Filesystem-backed CORSIA skills registry public API.

Skill discovery and precedence live in focused modules. This facade retains the
registry's public imports and renders the resolved inventory into context rows.
"""

from __future__ import annotations

from pathlib import Path

from core.effective_context import ContextSourceEntry
from core.lesson_index import index_line, partition
from core.role_context_sources import RoleContextResolution, RoleName
from core.skill_registry_types import SkillResolution, SkillRow, SkillScope
from core.skill_resolution import (
    _SYSTEM_UNAVAILABLE,
    normalize_skill_scope,
    local_skill_inventory,
    project_dir as _project_dir,
    resolve_effective_skills,
    skill_scope_root,
)
from infra.product_events import emit_product_event
from infra.system_package import system_package_ref


def _skills_index_content(
    rows: list[SkillRow],
    shadowed: list[str],
    unavailable: str | None = None,
    errors: list[SkillRow] | None = None,
) -> str:
    """Render the compact dynamic index delivered to an agent."""
    from tools.catalog import MCP_CORSIA_PREFIX

    lines = [
        "Available skills. Before answering, compare the user request to the skill descriptions below. "
        "If the user names a skill or the request matches a skill description, you MUST load that skill's "
        "SKILL.md before responding. Load the listed SKILL.md path with your file-read "
        f"tool: `{MCP_CORSIA_PREFIX}read_file` on Claude Code, `read_file` on other harnesses; "
        "when only the generic CORSIA tool is available, call it with name=read_file.",
    ]
    lines.extend(index_line(row) for row in rows)
    if errors:
        lines.extend(["", "Not loaded — skill errors (these sources are skipped until repaired):"])
        for row in sorted(errors, key=lambda row: (str(row.path), row.name)):
            lines.append(f"- {row.name} ({row.scope}, {row.path}):")
            lines.extend(f"  - {error}" for error in row.errors)
        lines.append(
            "Repair skills you own. Report inherited or shared skill errors to their owner; "
            "System errors require an upstream source fix."
        )
    if shadowed:
        count = len(shadowed)
        noun = "name" if count == 1 else "names"
        what = (
            "System skill {} unavailable, local copies not loaded"
            if unavailable
            else "skill {} shadowed by System"
        )
        lines.append(f"- {count} {what.format(noun)}: {', '.join(shadowed)}")
    return "\n".join(lines)


def _role_source_segment(role: RoleName | None) -> str:
    """Return the source-id segment for an optional role layer."""
    return f":{role}" if role else ""


def _row_source(row: SkillRow, *, freshness: str, reason: str) -> ContextSourceEntry:
    """Render an omitted or invalid skill through the established error surface."""
    return ContextSourceEntry(
        source_id=(
            f"skill-{row.status}:{row.scope}:{row.scope_id}"
            f"{_role_source_segment(row.role)}:{row.name}"
        ),
        origin_section="dynamic",
        label=f"{'Skill error' if row.status == 'error' else 'Skill'}: {row.name}",
        stable_reference=str(row.path),
        freshness=freshness,
        inclusion="omitted",
        reason=reason,
        owner_surface="document-viewer",
        metadata={
            "provider": "skills",
            "name": row.name,
            "scope": row.scope,
            "scope_id": row.scope_id,
            "path": str(row.path),
            "repair_owner": "skills",
            "errors": row.errors,
            "role": row.role,
        },
    )


def _system_unavailable_source() -> ContextSourceEntry:
    """Render the one fixed error row for unreadable System skills."""
    return ContextSourceEntry(
        source_id="skill-error:system:unavailable",
        origin_section="corsia",
        label="System skills",
        stable_reference=system_package_ref("skills"),
        freshness="error",
        inclusion="omitted",
        reason=_SYSTEM_UNAVAILABLE,
        owner_surface="skills",
        prompt_content=None,
        metadata={"provider": "system_skills", "repair_owner": "corsia-release"},
    )


def _listed_skills(included, lessons) -> list[dict]:
    """The skills in the index, with the trial lesson id where one is offered."""
    trial_ids = {
        (lesson["scope"], lesson["scope_id"], lesson["name"]): lesson["id"]
        for lesson in lessons
    }
    return [
        {
            "name": row.name,
            "scope": row.scope,
            "scope_id": row.scope_id,
            "role": row.role,
            "lesson_id": trial_ids.get((row.scope, row.scope_id, row.name)),
        }
        for row in included
    ]


def _displayable_skill_errors(rows: list[SkillRow]) -> list[SkillRow]:
    """Keep malformed skills separate from unavailable role-root diagnostics."""
    return [
        row for row in rows
        if row.errors != ["shadowed by System"]
        and not (row.errors and row.errors[0].startswith("role skill root unavailable:"))
    ]


def skill_context_sources(
    *,
    thread_id: str,
    project_id: str | None,
    workspace: Path,
    db,
    role_context: RoleContextResolution | None = None,
) -> list[ContextSourceEntry]:
    """Build context-source rows for the resolved skills index and diagnostics."""
    resolved = resolve_effective_skills(
        thread_id=thread_id,
        project_id=project_id,
        workspace=workspace,
        db=db,
        role_context=role_context,
    )
    # The one writer of lesson membership: a held trial leaves the index here.
    split = partition(resolved.included, resolved.omitted, thread_id=thread_id, db=db)
    included = sorted(
        split.offered,
        key=lambda row: (row.scope != "system", row.name),
    )
    shadowed = sorted(
        {row.name for row in split.omitted if row.errors == ["shadowed by System"]}
    )
    errors = _displayable_skill_errors(resolved.errors)
    has_index = bool(included or shadowed or errors)
    sources: list[ContextSourceEntry] = [
        ContextSourceEntry(
            source_id="dynamic:skills-index",
            origin_section="dynamic",
            label="Skills",
            stable_reference=f"thread://{thread_id}/skills",
            freshness="fresh",
            inclusion="included" if has_index else "omitted",
            reason="resolved skills index" if has_index else "no skills resolved",
            owner_surface="skills",
            prompt_content=(
                _skills_index_content(included, shadowed, resolved.system_unavailable, errors)
                if has_index
                else None
            ),
            metadata={
                "provider": "skills",
                "skill_count": len(included),
                "omitted_count": len(split.omitted),
                "error_count": len(resolved.errors),
                "system_unavailable": resolved.system_unavailable,
                "repair_owner": "skills",
                # Always a list here; a row without the key predates lessons.
                "lessons": split.lessons,
                # The listed skills by scope, for the context sheet. Absent on
                # a row that predates it: unavailable, never "none".
                "skills": _listed_skills(included, split.lessons),
            },
        )
    ]
    if resolved.system_unavailable:
        sources.append(_system_unavailable_source())
    for row in split.omitted:
        reason = row.errors[0] if row.errors else "skill overridden by a more specific scope"
        sources.append(_row_source(row, freshness="fresh", reason=reason))
    for row in resolved.errors:
        sources.append(
            _row_source(row, freshness="error", reason="skill validation failed")
        )
    emit_product_event(
        "skill_registry",
        "context_injected",
        thread_id=thread_id,
        properties={
            "project_id": project_id,
            "skill_count": len(included),
            "omitted_count": len(split.omitted),
            "error_count": len(resolved.errors),
        },
    )
    return sources
