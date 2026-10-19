"""Connected-account foundation: shared reach and account-local state."""

from __future__ import annotations

from connections.connected_accounts import (
    account_reaches_thread,
    create_google_account_for_thread,
)
from infra.database import get_db
from infra.time import utc_now_iso
from tests.registry_helpers import make_session


def test_project_caller_cannot_create_an_account_with_workspace_reach(isolated_db) -> None:
    db = get_db()
    try:
        now = utc_now_iso()
        db.execute(
            "INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)",
            ("project-a", "Project A", now, now),
        )
        db.commit()
        project_thread = make_session(db, project_id="project-a", prefix="project")
        workspace_thread = make_session(db, prefix="workspace")

        account = create_google_account_for_thread(
            "person@example.test", actor_thread_id=project_thread, created_by=("agent", project_thread), db=db,
        )

        assert account["reach"] == {
            "mode": "selected", "project_ids": ["project-a"], "outside_projects": False,
        }
        assert account_reaches_thread(str(account["id"]), project_thread, db=db)
        assert not account_reaches_thread(str(account["id"]), workspace_thread, db=db)
    finally:
        db.close()
