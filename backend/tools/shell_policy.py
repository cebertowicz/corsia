"""Repository policy snapshot selection for protected shell execution."""

from collections.abc import Callable

from infra.project_repo_policy import RepoPolicy


def load_shell_repo_policies(
    loader: Callable[..., list[RepoPolicy]],
    leased: bool,
    project_id: object,
) -> tuple[list[RepoPolicy], list[RepoPolicy]]:
    """Load global enforcement and backend-project grant snapshots."""
    if leased:
        return [], []
    policies = loader()
    if not policies or not isinstance(project_id, str) or not project_id:
        return policies, []
    return policies, loader(project_id=project_id)
