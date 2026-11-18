"""The front door for registering a repository.

Project settings (``api.project_repos.add_repo``) and the ``project_repo_register``
tool both register through :func:`register_source_repo_async`, which runs
``api.project_repos.register_source_repo`` once on a DB thread. A registration
never waits for or refuses over what agents are running (``api.project_repo_claim``);
it is refused only while another repository change is in progress.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from api.project_repo_controls import RepoAdd
from infra import db_async
from infra.repo_policy_transition import RepoPolicyTransitionBusy


async def register_source_repo_async(
    project_id: str, body: RepoAdd, *, checked: Path | None = None,
) -> dict:
    """Register the repository.

    Raises:
        HTTPException: 409 while another repository change is in progress; the add's
            own refusals as raised.
    """
    # Imported here: api.project_repos imports this module for its add route.
    from api.project_repos import register_source_repo

    try:
        return await db_async.run_in_db(
            lambda: register_source_repo(project_id, body, checked=checked),
        )
    except RepoPolicyTransitionBusy as exc:
        raise HTTPException(409, str(exc)) from exc
