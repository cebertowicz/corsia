"""Connected-account foundation: shared reach and account-local state."""

from __future__ import annotations

import pytest

from connections.connected_accounts import (
    GOOGLE_PROVIDER, ConnectedAccountError, account_reaches_thread, create_account, create_account_for_thread,
)
from connections.account_state import record_audit
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

        account = create_account_for_thread(GOOGLE_PROVIDER,
            "person@example.test", actor_thread_id=project_thread, created_by=("agent", project_thread), db=db,
        )

        assert account["reach"] == {
            "mode": "selected", "project_ids": ["project-a"], "outside_projects": False,
        }
        assert account_reaches_thread(str(account["id"]), project_thread, db=db)
        assert not account_reaches_thread(str(account["id"]), workspace_thread, db=db)
    finally:
        db.close()


def test_audit_records_only_fixed_account_lifecycle_facts(isolated_db) -> None:
    db = get_db()
    try:
        account = create_account(GOOGLE_PROVIDER,
            "person@example.test", reach={"mode": "all", "project_ids": [], "outside_projects": False},
            created_by=("system", "test"), db=db,
        )
        record_audit(
            str(account["id"]), "sign_out", ("agent", "agent-1"),
            revocation_cause="provider_timeout", db=db,
        )
        row = db.execute(
            "SELECT account_id,actor_kind,actor_id,action,revocation_cause FROM connected_account_audit",
        ).fetchone()
        assert tuple(row) == (str(account["id"]), "agent", "agent-1", "sign_out", "provider_timeout")
        assert set(row.keys()) == {"account_id", "actor_kind", "actor_id", "action", "revocation_cause"}
        with pytest.raises(ConnectedAccountError):
            record_audit(str(account["id"]), "free-text", ("agent", "agent-1"), db=db)
        with pytest.raises(ConnectedAccountError):
            record_audit(
                str(account["id"]), "remove", ("agent", "agent-1"),
                revocation_cause="provider said arbitrary things", db=db,
            )
    finally:
        db.close()
