"""Shared data models for the filesystem-backed skills registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core.role_context_sources import RoleName

SkillScope = Literal["workspace", "project", "thread", "system"]


@dataclass(frozen=True)
class SkillRow:
    """One discovered skill and its resolution status."""

    name: str
    description: str
    scope: SkillScope
    scope_id: str
    path: Path
    body_path: Path
    metadata: dict[str, Any]
    status: Literal["included", "omitted", "error"]
    errors: list[str] = field(default_factory=list)
    overrides: str | None = None
    role: RoleName | None = None
    # CORSIA data (handler, workflow) from the sidecar or legacy block; see SkillValidation.
    corsia: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the API-safe dictionary projection for this skill row."""
        return {
            "name": self.name,
            "description": self.description,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "path": str(self.path),
            "body_path": str(self.body_path),
            "metadata": self.metadata,
            "status": self.status,
            "errors": list(self.errors),
            "overrides": self.overrides,
            "role": self.role,
        }


@dataclass(frozen=True)
class SkillResolution:
    """The included, omitted, and invalid skills for one effective scope."""

    included: list[SkillRow]
    omitted: list[SkillRow]
    errors: list[SkillRow]
    system_unavailable: str | None = None
    precedence: tuple[tuple[str, str], ...] = ()

    def all_rows(self) -> list[SkillRow]:
        """Return every resolved row, preserving included/omitted/error buckets."""
        return [*self.included, *self.omitted, *self.errors]
