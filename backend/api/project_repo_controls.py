"""What a repository add or update request may set, checked before anything changes."""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from infra.project_repo_policy import RepoPolicy


class RepoAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str  # "link" | "create"
    path: str | None = None  # link mode: the existing directory to attach
    name: str | None = None  # optional for link (defaults from basename); required for create
    pinned_branch: str | None = None
    protect_base_checkout: bool | None = None


class RepoUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned_branch: str | None = None
    protect_base_checkout: bool | None = None


def add_controls(
    body: RepoAdd,
    *,
    is_git: bool,
    current_branch: str | None,
) -> tuple[str | None, bool]:
    fields = body.model_fields_set
    if (
        "protect_base_checkout" in fields
        and body.protect_base_checkout is None
    ):
        raise HTTPException(400, "protect_base_checkout must be true or false")
    if not is_git:
        if (
            ("pinned_branch" in fields and body.pinned_branch is not None)
            or (
                "protect_base_checkout" in fields
                and body.protect_base_checkout
            )
        ):
            raise HTTPException(
                409,
                "Branch pinning and checkout protection require a Git repository",
            )
        return None, False
    pinned = (
        body.pinned_branch
        if "pinned_branch" in fields
        else current_branch
    )
    protected = (
        bool(body.protect_base_checkout)
        if "protect_base_checkout" in fields
        else True
    )
    return pinned, protected


def update_controls(
    body: RepoUpdate,
    *,
    policy: RepoPolicy,
) -> tuple[str | None, bool]:
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(400, "No repository settings to update")
    if (
        "protect_base_checkout" in fields
        and body.protect_base_checkout is None
    ):
        raise HTTPException(400, "protect_base_checkout must be true or false")
    return (
        (
            body.pinned_branch
            if "pinned_branch" in fields
            else policy.pinned_branch
        ),
        (
            bool(body.protect_base_checkout)
            if "protect_base_checkout" in fields
            else policy.protect_base_checkout
        ),
    )
