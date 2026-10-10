"""Project API request models and validation helpers."""

from __future__ import annotations

import re

from fastapi import HTTPException
from pydantic import BaseModel


VALID_PROJECT_STATUSES = {"idea", "exploring", "validated", "active", "archived"}

PROJECT_TRANSITIONS: dict[str, list[str]] = {
    "idea": ["exploring", "active", "archived"],
    "exploring": ["validated", "idea", "archived"],
    "validated": ["active", "exploring", "archived"],
    "active": ["archived", "validated"],
    "archived": ["active", "idea"],
}

_ACCENT_COLOR_RE = re.compile(
    r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$"
    r"|^(?:rgb|rgba|hsl|hsla)\([0-9.,%\s/]+\)$",
    re.IGNORECASE,
)


def validate_accent_color(value: str | None) -> None:
    if value is None:
        return
    if not _ACCENT_COLOR_RE.match(value.strip()):
        raise HTTPException(
            400, "accent_color must be a hex (#rrggbb) or hsl()/rgb() color, or null"
        )


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    path: str | None = None
    repo_path: str | None = None
    status: str = "active"
    accent_color: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    path: str | None = None
    status: str | None = None
    accent_color: str | None = None
    bootloader_files: list[str] | None = None


class ProjectTransition(BaseModel):
    to_status: str
    reason: str = ""
