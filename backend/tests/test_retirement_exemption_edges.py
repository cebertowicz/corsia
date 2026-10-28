"""Edges of the retirement exemption that its main tests do not reach.

A row's stored location never decides its retirement, a user comment shows
the owner a shared document whose row an ownership change settled, and the
ended pull request reads the same in Python and SQL.
"""

from __future__ import annotations

import sqlite3

import pytest

from infra import config
from infra.database import get_db, write_transaction
from infra.github_pr_identity import GITHUB_INTEGRATION, PR_TYPE
from infra.platform_paths import WorkspaceSelection
from registry import models, node_retirement, retirement_predicate
from tests.registry_helpers import make_session

_WS = "workspace://ws-1"


@pytest.fixture()
def db(isolated_db, tmp_path, monkeypatch):
    workspace = (tmp_path / "workspace").resolve()
    for folder in ("shared", "projects/alpha"):
        (workspace / folder).mkdir(parents=True)
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(
        config, "WORKSPACE_SELECTION",
        WorkspaceSelection(workspace_id="ws-1", root=workspace, name="Test workspace"),
    )
    connection = get_db()
    connection.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) "
        "VALUES ('alpha','alpha',?,'t0','t0')",
        (f"{_WS}/projects/alpha",),
    )
    connection.commit()
    return connection


def _document(db, thread_id: str, relative: str) -> dict:
    return models.register_artifact(
        integration="workspace", type="document", external_id=f"{_WS}/{relative}",
        path=str(config.WORKSPACE / relative),
        parent_thread_id=thread_id, title=relative.rsplit("/", 1)[-1],
        retention="durable", metadata={"zone": "workspace"}, db=db,
    )


def _row(db, thread_id: str, artifact_id: str) -> sqlite3.Row:
    return db.execute(
        "SELECT id,surfacing,retired_at FROM artifact_registrations "
        "WHERE parent_thread_id=? AND artifact_id=?",
        (thread_id, artifact_id),
    ).fetchone()


def test_retirement_reads_no_stored_location_or_project_row(db):
    """Nothing in a row's location decides retirement any more: a location
    pathlib cannot expand, or a project row left over from another workspace,
    used to spare every row or fail every settlement."""
    thread_id = make_session(db, prefix="any-location")
    note = _document(db, thread_id, "projects/alpha/.threads/t1/notes.md")
    shared = _document(db, thread_id, "shared/planning/roadmap.md")
    db.execute(
        "UPDATE artifact_registrations SET origin_plan_node_id='node-1' "
        "WHERE parent_thread_id=?",
        (thread_id,),
    )
    db.execute(
        "UPDATE artifact_identities SET path='~no-such-user-for-this-test/notes.md' WHERE id=?",
        (note["id"],),
    )
    db.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) "
        "VALUES ('moved','moved',?,'t0','t0')",
        (_WS.replace("ws-1", "ws-other") + "/projects/moved",),
    )
    db.commit()

    assert node_retirement.retire_registrations_for_node(
        db, node_id="node-1", trigger="test", causal_event_id=None,
    ) == 2
    db.commit()
    for artifact in (note, shared):
        assert _row(db, thread_id, artifact["id"])["retired_at"] is not None


def test_a_user_comment_shows_a_shared_document_its_ownership_change_settled(db):
    from doceditor import feedback_comments

    thread_id = make_session(db, prefix="owner")
    shared = _document(db, thread_id, "projects/alpha/shared/documents/REFERENCE.md")
    node_retirement.retire_ownership_row(
        db, artifact_id=shared["id"], thread_id=thread_id, trigger="test",
    )
    db.commit()
    assert _row(db, thread_id, shared["id"])["surfacing"] == "settled"

    with write_transaction(db, scope="test_user_comment") as connection:
        connection.execute(
            "INSERT INTO doc_comments "
            "(id,artifact_id,author,author_kind,body,created_at,updated_at) "
            "VALUES ('c-1',?,'user','user','Check this.','t0','t0')",
            (shared["id"],),
        )
        feedback_comments.make_user_document_durable(
            connection, {"artifact_id": shared["id"], "author_kind": "user"},
        )

    assert _row(db, thread_id, shared["id"])["surfacing"] == "visible"
    last = db.execute(
        "SELECT event_type FROM artifact_registration_events "
        "WHERE artifact_id=? ORDER BY rowid DESC LIMIT 1",
        (shared["id"],),
    ).fetchone()
    assert last["event_type"] == "exempt_row_restored"


@pytest.mark.parametrize(
    ("integration", "type_", "metadata"),
    [
        (GITHUB_INTEGRATION, PR_TYPE, '{"pr_state": "merged"}'),
        (GITHUB_INTEGRATION, PR_TYPE, '{"pr_state": "closed"}'),
        (GITHUB_INTEGRATION, PR_TYPE, '{"pr_state": "open"}'),
        (GITHUB_INTEGRATION, PR_TYPE, "{}"),
        (GITHUB_INTEGRATION, PR_TYPE, None),
        (GITHUB_INTEGRATION, "issue", '{"pr_state": "closed"}'),
        ("gmail", PR_TYPE, '{"pr_state": "merged"}'),
    ],
)
def test_identity_ended_agrees_with_the_ended_pull_request_clause(integration, type_, metadata):
    """Routing reads an identity row in Python; storage seams read it in SQL."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE artifact_identities (integration TEXT, type TEXT, metadata TEXT)")
    connection.execute("INSERT INTO artifact_identities VALUES (?, ?, ?)",
                       (integration, type_, metadata))
    [row] = connection.execute(
        f"SELECT i.*, {retirement_predicate.ENDED_PULL_REQUEST} AS ended "
        "FROM artifact_identities AS i").fetchall()
    assert retirement_predicate.identity_ended(dict(row)) is bool(row["ended"])
