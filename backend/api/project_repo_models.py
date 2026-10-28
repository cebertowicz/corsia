"""Request and response shapes for project repository bindings."""
from pathlib import Path

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


def with_policy(repo: dict, policies: dict[str, RepoPolicy]) -> dict:
    """Attach policy state only when the visible binding still matches its row."""
    row = dict(repo)
    policy = policies.get(repo["name"])
    bound = (
        policy is not None
        and not repo.get("missing", False)
        and policy.path
        == Path(str(repo["path"])).expanduser().resolve(strict=False)
    )
    row["registered"] = bound
    row["pinned_branch"] = policy.pinned_branch if bound and policy is not None else None
    row["protect_base_checkout"] = (
        policy.protect_base_checkout if bound and policy is not None else False
    )
    # Current clients still render this derived response field. Mutations use
    # only the two canonical controls above.
    row["branch_pinned"] = bool(bound and policy is not None and policy.branch_pinned)
    return row
