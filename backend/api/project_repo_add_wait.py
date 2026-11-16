"""The front door for registering a repository: wait out the writes it meets.

Project settings (``api.project_repos.add_repo``) and the ``project_repo_register``
tool both register through :func:`register_source_repo_waiting`. Each attempt runs
``api.project_repos.register_source_repo`` on a DB thread, which enters the repo
policy transition afresh; between attempts it waits on the event loop
(``infra.repo_policy_wait.wait_for_transition``), holding no thread or lock.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from api.project_repo_controls import RepoAdd
from infra import db_async
from infra.repo_policy_transition import RepoPolicyTransitionBusy
from infra.repo_policy_wait import wait_for_transition


async def register_source_repo_waiting(
    project_id: str, body: RepoAdd, *, checked: Path | None = None,
) -> dict:
    """Register, waiting for in-flight writes the registration meets to finish.

    Raises:
        HTTPException: 409 naming what is still running once the wait is over, or at
            once for a conflict waiting cannot end; the add's own refusals as raised.
    """
    # Imported here: api.project_repos imports this module for its add route.
    from api.project_repos import register_source_repo

    try:
        return await wait_for_transition(
            lambda: db_async.run_in_db(
                lambda: register_source_repo(project_id, body, checked=checked),
            ),
        )
    except RepoPolicyTransitionBusy as exc:
        raise HTTPException(409, str(exc)) from exc
