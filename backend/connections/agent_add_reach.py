"""The one reach rule for apps and accounts an agent adds."""

from __future__ import annotations

import sqlite3

from connections.account_policy import missing_project_reason, thread_scope
from connections.app_reach import AppReach
from infra.corsia_agent import is_corsia_agent


class AddingScopeError(ValueError):
    """The server cannot safely derive the adding agent's reach."""


def reach_for_adding_thread(thread_id: str, *, db: sqlite3.Connection) -> AppReach:
    """Derive an added app or account's reach from the server-known thread scope."""
    scope = thread_scope(thread_id, db=db)
    if missing_project_reason(scope, db=db):
        raise AddingScopeError("adding scope is unavailable")
    if is_corsia_agent(thread_id, db=db):
        return AppReach(mode="all", project_ids=frozenset(), outside_projects=False)
    project_id = scope["project_id"]
    return AppReach(
        mode="selected",
        project_ids=frozenset() if project_id is None else frozenset((str(project_id),)),
        outside_projects=project_id is None,
    )
