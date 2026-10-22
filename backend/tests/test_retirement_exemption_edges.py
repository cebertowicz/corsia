"""Edges of the retirement exemption that its main tests do not reach.

One location that cannot be classified is spared, shared roots that cannot be
read are an error rather than an answer, every consumer of the exemption
clause works on a connection nothing has prepared, and a user comment shows
the owner a shared document whose row an ownership change settled.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from infra import config
from infra.database import get_db, write_transaction
from infra.github_pr_identity import GITHUB_INTEGRATION, PR_TYPE
from infra.platform_paths import WorkspaceSelection
from registry import (
    exemption_restore,
    finished_task_row_backfill,
    models,
    node_retirement,
    retirement_predicate,
    shared_principals,
    view,
)
from registry.retirement_predicate import RETIREMENT_NOT_EXEMPT_CLAUSE
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
    # The warned-locations set lasts for the process; each test starts with none.
    monkeypatch.setattr(retirement_predicate, "_unclassifiable", set())
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


def test_a_location_that_cannot_be_classified_is_spared(db, monkeypatch, caplog):
    thread_id = make_session(db, prefix="unclassified")
    note = _document(db, thread_id, "projects/alpha/.threads/t1/notes.md")
    db.execute(
        "UPDATE artifact_registrations SET origin_plan_node_id='node-1' WHERE id=?",
        (_row(db, thread_id, note["id"])["id"],),
    )
    db.commit()

    def fail(_path, _roots):
        raise OSError("cannot resolve")

    monkeypatch.setattr(shared_principals, "is_under_shared_roots", fail)
    with caplog.at_level(logging.WARNING, logger=retirement_predicate.__name__):
        retired = node_retirement.retire_registrations_for_node(
            db, node_id="node-1", trigger="test", causal_event_id=None,
        )
    db.commit()

    assert retired == 0
    assert _row(db, thread_id, note["id"])["retired_at"] is None
    assert caplog.text.count("shared-document check failed") == 1

    # The pane classifies every row on every read: one warning per location.
    with caplog.at_level(logging.WARNING, logger=retirement_predicate.__name__):
        node_retirement.retire_registrations_for_node(
            db, node_id="node-1", trigger="test", causal_event_id=None,
        )
    assert caplog.text.count("shared-document check failed") == 1


def test_a_classifier_failure_that_is_not_about_the_location_stays_loud(db, monkeypatch):
    thread_id = make_session(db, prefix="classifier")
    note = _document(db, thread_id, "projects/alpha/.threads/t1/notes.md")
    db.execute(
        "UPDATE artifact_registrations SET origin_plan_node_id='node-1' WHERE id=?",
        (_row(db, thread_id, note["id"])["id"],),
    )
    db.commit()

    def fail(_path, _roots):
        raise RecursionError("classifier broke")

    monkeypatch.setattr(shared_principals, "is_under_shared_roots", fail)
    # SQLite reports an exception from a connection function as its own error.
    with pytest.raises(sqlite3.OperationalError):
        node_retirement.retire_registrations_for_node(
            db, node_id="node-1", trigger="test", causal_event_id=None,
        )
    db.rollback()
    assert _row(db, thread_id, note["id"])["retired_at"] is None
    assert retirement_predicate._unclassifiable == set()


def test_a_home_folder_that_cannot_be_expanded_is_spared(db, monkeypatch):
    thread_id = make_session(db, prefix="unknown-home")
    note = _document(db, thread_id, "projects/alpha/.threads/t1/notes.md")
    db.execute(
        "UPDATE artifact_registrations SET origin_plan_node_id='node-1' WHERE id=?",
        (_row(db, thread_id, note["id"])["id"],),
    )
    db.execute(
        "UPDATE artifact_identities SET path='~no-such-user-for-this-test/notes.md' WHERE id=?",
        (note["id"],),
    )
    db.commit()

    retired = node_retirement.retire_registrations_for_node(
        db, node_id="node-1", trigger="test", causal_event_id=None,
    )

    assert retired == 0
    assert _row(db, thread_id, note["id"])["retired_at"] is None


def test_roots_that_cannot_be_read_are_an_error_for_every_consumer(db):
    thread_id = make_session(db, prefix="unresolved")
    note = _document(db, thread_id, "projects/alpha/.threads/t1/notes.md")
    db.execute(
        "UPDATE artifact_registrations SET origin_plan_node_id='node-1' WHERE id=?",
        (_row(db, thread_id, note["id"])["id"],),
    )
    # A project row left over from another workspace.
    db.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) "
        "VALUES ('moved','moved','workspace://ws-other/projects/moved','t0','t0')"
    )
    db.commit()

    # Neither answer is safe: retirement acts on "not shared", unpin on "shared".
    with pytest.raises(retirement_predicate.SharedRootsUnreadable):
        node_retirement.retire_registrations_for_node(
            db, node_id="node-1", trigger="test", causal_event_id=None,
        )
    with pytest.raises(retirement_predicate.SharedRootsUnreadable):
        retirement_predicate.retirement_to_restore(
            db, _row(db, thread_id, note["id"])["id"],
        )
    assert _row(db, thread_id, note["id"])["retired_at"] is None
    # An ownership retirement ignores the exemption, so it reads no roots.
    owned = _document(db, thread_id, "projects/alpha/.threads/t1/owned.md")
    assert node_retirement.retire_ownership_row(
        db, artifact_id=owned["id"], thread_id=thread_id, trigger="test",
    ) == 1
    db.commit()

    # Once the row is gone the roots read again and the task's file leaves.
    db.execute("DELETE FROM projects WHERE id='moved'")
    db.commit()
    assert node_retirement.retire_registrations_for_node(
        db, node_id="node-1", trigger="test", causal_event_id=None,
    ) == 1


def test_binding_reports_an_error_that_is_not_a_missing_function():
    class Locked:
        created = False

        def execute(self, _sql):
            raise sqlite3.OperationalError("database is locked")

        def create_function(self, *_args):
            self.created = True

    connection = Locked()
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        retirement_predicate.bind_shared_documents(connection)
    assert connection.created is False


@pytest.mark.parametrize(
    "consumer",
    [
        lambda fresh, thread_id, row: view._not_exempt_artifact_ids(fresh, thread_id),
        lambda fresh, thread_id, row: retirement_predicate.retirement_to_restore(
            fresh, row["id"],
        ),
        lambda fresh, thread_id, row: exemption_restore.restore_exempt_owner_row(
            fresh, artifact_id=row["artifact_id"], trigger="test",
        ),
        lambda fresh, thread_id, row: finished_task_row_backfill.retire_finished_task_rows(
            fresh,
        ),
        lambda fresh, thread_id, row: node_retirement.retire_registrations_for_node(
            fresh, node_id="node-1", trigger="test", causal_event_id=None,
        ),
        lambda fresh, thread_id, row: node_retirement.retire_taskless_rows(
            fresh, thread_id=thread_id, started_at="2999-01-01T00:00:00+00:00",
            trigger="test",
        ),
    ],
    ids=["pane", "unpin", "restore", "finished-task pass", "node settlement", "taskless"],
)
def test_every_consumer_of_the_clause_prepares_its_own_connection(db, consumer):
    thread_id = make_session(db, prefix="fresh")
    shared = _document(db, thread_id, "shared/planning/roadmap.md")
    row = db.execute(
        "SELECT id,artifact_id FROM artifact_registrations WHERE artifact_id=?",
        (shared["id"],),
    ).fetchone()
    db.commit()

    fresh = get_db()
    try:
        # Nothing has prepared this connection: the clause alone cannot run on it.
        with pytest.raises(sqlite3.OperationalError, match="no such function"):
            fresh.execute(
                f"SELECT 1 FROM artifact_registrations AS r WHERE {RETIREMENT_NOT_EXEMPT_CLAUSE}"
            ).fetchall()
        consumer(fresh, thread_id, row)
        fresh.commit()
    finally:
        fresh.close()
    # The shared document is exempt whichever consumer ran.
    assert _row(db, thread_id, shared["id"])["retired_at"] is None


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
