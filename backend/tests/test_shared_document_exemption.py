"""Only shared-tree documents outlive the task that touched them.

A durable file anywhere else in the workspace, a thread's own folder
included, is task-scoped: it leaves the pane when its task settles, and pin
keeps it. "Shared" is the one definition ownership and the pane use
(``registry.shared_principals.shared_roots``), so a project row or an identity
stored in an older form is shared exactly when they say it is, and the host
folder the workspace happens to live under can never widen it.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, unquote

import pytest

from infra import config
from infra.database import get_db
from infra.platform_paths import WorkspaceSelection
from plans import service as plans
from registry import models, pinning, shared_principals, view
from registry.node_retirement import retire_registrations_for_node
from registry.registration_writer import RegistrationOrigin
from registry.retirement_predicate import (
    RETIREMENT_NOT_EXEMPT_CLAUSE,
    bind_shared_documents,
)
from tests.registry_helpers import make_session
from tests.plan_writer_authorizer import raw_plan_seed

_WS = "workspace://ws-1"
_PROJECT = f"{_WS}/projects/alpha"
_ENCODED_PROJECT = f"{_WS}/projects/my%20proj"


@pytest.fixture()
def db(isolated_db, tmp_path, monkeypatch):
    # The workspace lives under a host folder named Shared on purpose.
    workspace = (tmp_path / "Shared" / "workspace").resolve()
    for folder in ("shared", "projects/alpha", "projects/my proj", "projects/legacy"):
        (workspace / folder).mkdir(parents=True)
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(
        config, "WORKSPACE_SELECTION",
        WorkspaceSelection(workspace_id="ws-1", root=workspace, name="Test workspace"),
    )
    connection = get_db()
    projects = (
        ("alpha", _PROJECT),
        ("spaced", _ENCODED_PROJECT),
        # A project row from before logical references: an absolute path.
        ("legacy", str(workspace / "projects" / "legacy")),
        ("bare", None),
    )
    for project_id, path in projects:
        connection.execute(
            "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES (?,?,?,'t0','t0')",
            (project_id, project_id, path),
        )
    connection.commit()
    return connection


def _reference(relative: str) -> str:
    return f"{_WS}/{quote(relative)}"


def _location(external_id: str) -> Path:
    """Where a logical identity's file lives in the test workspace."""
    return config.WORKSPACE / unquote(external_id[len(_WS) + 1:])


def _is_exempt(db, artifact_id: str) -> bool:
    bind_shared_documents(db)
    row = db.execute(
        f"""SELECT 1 FROM artifact_registrations AS r
             WHERE r.artifact_id=? AND {RETIREMENT_NOT_EXEMPT_CLAUSE}""",
        (artifact_id,),
    ).fetchone()
    return row is None


def _exempt(db, relative: str, *, zone: str = "workspace", retention: str = "durable") -> bool:
    thread_id = make_session(db, prefix="shared-exempt")
    external_id = _reference(relative)
    artifact = models.register_artifact(
        integration="workspace", type="document", external_id=external_id,
        path=str(_location(external_id)),
        parent_thread_id=thread_id, title="doc.md", retention=retention,
        metadata={"zone": zone}, db=db,
    )
    return _is_exempt(db, artifact["id"])


def _exempt_legacy_identity(db, external_id: str) -> bool:
    """An identity from before logical references: an absolute path, no stored path."""
    thread_id = make_session(db, prefix="legacy-exempt")
    artifact = models.register_artifact(
        integration="workspace", type="document", external_id=external_id,
        parent_thread_id=thread_id, title="doc.md", retention="durable",
        metadata={"zone": "workspace"}, db=db,
    )
    return _is_exempt(db, artifact["id"])


@pytest.mark.parametrize(
    "relative",
    [
        "shared/planning/roadmap.md",
        "projects/alpha/shared/documents/DECISIONS.md",
        "projects/my proj/shared/a.md",
        # The project row stores an absolute path, not a logical reference.
        "projects/legacy/shared/a.md",
    ],
)
def test_documents_in_a_shared_tree_are_exempt(db, relative):
    assert _exempt(db, relative) is True


@pytest.mark.parametrize(
    "relative",
    [
        "projects/alpha/.threads/t1/notes.md",
        "projects/alpha/.threads/t1/scratch/query.sql",
        "projects/alpha/.threads/t1/shared/notes.md",
        "projects/alpha/.threads/t1/skills/audit/SKILL.md",
        "projects/alpha/context/PROJECT.md",
        "USER.md",
        # No project row names this folder.
        "projects/beta/shared/a.md",
    ],
)
def test_every_other_workspace_file_is_task_scoped(db, relative):
    assert _exempt(db, relative) is False


def test_a_legacy_absolute_identity_is_shared_only_under_a_shared_root(db):
    workspace = config.WORKSPACE
    assert _exempt_legacy_identity(db, str(workspace / "shared/planning/roadmap.md")) is True
    assert _exempt_legacy_identity(db, str(workspace / "projects/alpha/shared/a.md")) is True
    assert _exempt_legacy_identity(
        db, str(workspace / "projects/alpha/.threads/t1/notes.md"),
    ) is False
    # A host folder named Shared above some other workspace is not a shared root.
    assert _exempt_legacy_identity(db, "/srv/Shared/ws/shared/planning/roadmap.md") is False
    assert _exempt_legacy_identity(
        db, "/srv/Shared/ws/projects/alpha/.threads/t1/notes.md",
    ) is False


def test_a_shared_path_outside_the_durable_workspace_kind_is_task_scoped(db):
    assert _exempt(db, "shared/a.md", zone="repo") is False
    assert _exempt(db, "shared/b.md", retention="ephemeral") is False


def test_a_document_the_user_commented_on_stays_exempt_wherever_it_lives(db):
    relative = "projects/alpha/.threads/t1/draft.md"
    assert _exempt(db, relative) is False
    artifact_id = db.execute(
        "SELECT id FROM artifact_identities WHERE external_id=?", (_reference(relative),),
    ).fetchone()["id"]

    for comment_id, author_kind, expected in (("c-agent", "agent", False), ("c-user", "user", True)):
        db.execute(
            "INSERT INTO doc_comments "
            "(id,artifact_id,author,author_kind,body,created_at,updated_at) "
            "VALUES (?,?,?,?,'Check this.','t0','t0')",
            (comment_id, artifact_id, author_kind, author_kind),
        )
        assert _is_exempt(db, artifact_id) is expected, author_kind


def _bound(db, thread_id: str, node, external_id: str) -> dict:
    turn_id = f"turn-{node.id}"
    db.execute(
        """INSERT OR IGNORE INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
            plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role)
           VALUES (?,?,0,'native/test','running','t0','boot',?,?,?,?,?)""",
        (turn_id, thread_id, node.plan_id, node.id, f"attempt-{turn_id}",
         node.commitment_id, "execution"),
    )
    db.commit()
    return models.register_artifact(
        integration="workspace", type="document", external_id=external_id,
        path=str(_location(external_id)),
        parent_thread_id=thread_id, title=external_id.rsplit("/", 1)[-1],
        retention="durable", metadata={"zone": "workspace"},
        origin=RegistrationOrigin(thread_id, turn_id, "artifact_tool"), db=db,
    )


def _rows(db, thread_id: str) -> dict[str, tuple[str, bool]]:
    return {
        row["artifact_id"]: (row["surfacing"], row["retired_at"] is not None)
        for row in db.execute(
            "SELECT artifact_id,surfacing,retired_at FROM artifact_registrations "
            "WHERE parent_thread_id=?",
            (thread_id,),
        ).fetchall()
    }


def test_settling_a_task_clears_thread_folder_files_and_keeps_shared_documents(db):
    thread_id = make_session(db, prefix="rule2")
    plan = plans.ensure_plan("thread", thread_id, db=db)
    node = plans.add_node(
        plan.id, kind="task", title="Write", acceptance_criteria="Written", db=db,
    )
    node = plans.transition_node(node.id, "active", db=db)
    note = _bound(db, thread_id, node, f"{_PROJECT}/.threads/t1/notes.md")
    scratch = _bound(db, thread_id, node, f"{_PROJECT}/.threads/t1/scratch/q.sql")
    kept = _bound(db, thread_id, node, f"{_PROJECT}/.threads/t1/pinned.md")
    shared = _bound(db, thread_id, node, f"{_PROJECT}/shared/documents/REFERENCE.md")
    pinning.update_pinned(kept["id"], pinned=True, parent_thread_id=thread_id, db=db)

    retire_registrations_for_node(db, node_id=node.id, trigger="test", causal_event_id=None)
    db.commit()

    rows = _rows(db, thread_id)
    assert rows[note["id"]] == ("settled", True)
    assert rows[scratch["id"]] == ("settled", True)
    assert rows[kept["id"]] == ("visible", True)
    assert rows[shared["id"]] == ("visible", False)
    shown = {n["id"] for n in view.pane_for_thread(thread_id)["nodes"] if n["kind"] == "artifact"}
    assert shown == {kept["id"], shared["id"]}

    # Unpin returns the thread-folder file to the retirement its task left.
    pinning.update_pinned(kept["id"], pinned=False, parent_thread_id=thread_id, db=db)
    assert _rows(db, thread_id)[kept["id"]] == ("settled", True)


def test_the_predicate_agrees_with_the_shared_roots_definition(db, monkeypatch):
    from registry import file_resolution, service

    workspace = config.WORKSPACE
    monkeypatch.setattr(file_resolution, "get_allowed_dirs_rw", lambda **_kwargs: [workspace])
    thread_id = make_session(db, prefix="parity")
    relative = [
        "shared/planning/roadmap.md",
        "projects/alpha/shared/documents/DECISIONS.md",
        "projects/alpha/.threads/t1/notes.md",
        "projects/alpha/.threads/t1/shared/notes.md",
        "projects/alpha/context/PROJECT.md",
        # A project row that stores an absolute path.
        "projects/legacy/shared/a.md",
        "projects/legacy/.threads/t1/notes.md",
        "USER.md",
    ]
    shared = set()

    for name in relative:
        path = workspace / name
        artifact = service.register_write_strict(thread_id, str(path), None, db=db)
        expected = shared_principals.is_shared_document_path(path, workspace=workspace, db=db)
        assert _is_exempt(db, artifact["id"]) is expected, name
        if expected:
            shared.add(name)

    assert shared == {
        "shared/planning/roadmap.md",
        "projects/alpha/shared/documents/DECISIONS.md",
        "projects/legacy/shared/a.md",
    }
    # An identity stored as an absolute path, with no stored path beside it.
    for name in ("shared/legacy-identity.md", "projects/alpha/.threads/t1/legacy-identity.md"):
        path = workspace / name
        expected = shared_principals.is_shared_document_path(path, workspace=workspace, db=db)
        assert _exempt_legacy_identity(db, str(path)) is expected, name


def test_a_user_comment_brings_back_the_row_its_task_already_retired(db, monkeypatch):
    from doceditor import feedback_comments
    from infra.database import write_transaction
    from plans import public

    nudges: list = []
    monkeypatch.setattr(public, "deliver_notifications", nudges.extend)

    thread_id = make_session(db, prefix="comment-restore")
    node = _node(db, thread_id, "Draft")
    draft = _bound(db, thread_id, node, f"{_PROJECT}/.threads/t1/draft.md")
    removed = _bound(db, thread_id, node, f"{_PROJECT}/.threads/t1/removed.md")
    retire_registrations_for_node(db, node_id=node.id, trigger="test", causal_event_id=None)
    db.execute(
        "UPDATE artifact_registrations SET surfacing='quiet' WHERE artifact_id=?",
        (removed["id"],),
    )
    db.commit()
    assert _rows(db, thread_id)[draft["id"]] == ("settled", True)

    for artifact in (draft, removed):
        comment = {"artifact_id": artifact["id"], "author_kind": "agent"}
        feedback_comments.make_user_document_durable(db, comment)
    db.commit()
    # An agent's comment exempts nothing, so nothing comes back.
    assert _rows(db, thread_id)[draft["id"]] == ("settled", True)

    with write_transaction(db, scope="test_user_comment") as connection:
        for artifact in (draft, removed):
            connection.execute(
                "INSERT INTO doc_comments "
                "(id,artifact_id,author,author_kind,body,created_at,updated_at) "
                "VALUES (?,?,'user','user','Check this.','t0','t0')",
                (f"c-{artifact['id']}", artifact["id"]),
            )
            comment = {"artifact_id": artifact["id"], "author_kind": "user"}
            feedback_comments.make_user_document_durable(connection, comment)
        # The owner's pane is told only once the comment has committed.
        assert nudges == []

    rows = _rows(db, thread_id)
    assert rows[draft["id"]] == ("visible", False)
    # The user's own removal is not a retirement, so a comment does not undo it.
    assert rows[removed["id"]][0] == "quiet"
    shown = {n["id"] for n in view.pane_for_thread(thread_id)["nodes"] if n["kind"] == "artifact"}
    assert shown == {draft["id"]}
    events = [
        row["event_type"] for row in db.execute(
            "SELECT event_type FROM artifact_registration_events "
            "WHERE artifact_id=? ORDER BY rowid", (draft["id"],),
        ).fetchall()
    ]
    assert events[-1] == "exempt_row_restored"
    assert [(frame["artifact_id"], frame["affected_thread_ids"]) for _user, frame in nudges] == [
        (draft["id"], [thread_id]),
    ]
    # A second comment finds nothing left to restore.
    feedback_comments.make_user_document_durable(db, comment | {"artifact_id": draft["id"]})
    assert db.execute(
        "SELECT COUNT(*) AS n FROM artifact_registration_events "
        "WHERE artifact_id=? AND event_type='exempt_row_restored'", (draft["id"],),
    ).fetchone()["n"] == 1


async def test_a_restored_row_reaches_an_open_pane_when_the_comment_is_written_on_a_worker(
    db, monkeypatch,
):
    """Every comment adapter writes on a database worker, where there is no event
    loop: the pane update must still reach the owner's open socket."""
    import asyncio

    from doceditor import feedback_comments
    from infra import app_loop
    from infra.database import write_transaction
    from infra.db_async import run_in_db
    from infra.state import ws as sockets

    thread_id = make_session(db, prefix="comment-restore-worker")
    node = _node(db, thread_id, "Draft")
    draft = _bound(db, thread_id, node, f"{_PROJECT}/.threads/t1/draft.md")
    retire_registrations_for_node(db, node_id=node.id, trigger="test", causal_event_id=None)
    db.commit()
    assert _rows(db, thread_id)[draft["id"]] == ("settled", True)
    user_id = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()[0]

    received: list[dict] = []

    class Socket:
        async def send_json(self, message: dict) -> None:
            received.append(message)

    monkeypatch.setitem(sockets._ws_user_clients, user_id, [Socket()])
    monkeypatch.setattr(app_loop, "_LOOP", None)
    app_loop.capture(asyncio.get_running_loop())

    def comment() -> None:
        connection = get_db()
        with write_transaction(connection, scope="test_user_comment") as write:
            write.execute(
                "INSERT INTO doc_comments "
                "(id,artifact_id,author,author_kind,body,created_at,updated_at) "
                "VALUES ('c-worker',?,'user','user','Check this.','t0','t0')",
                (draft["id"],),
            )
            feedback_comments.make_user_document_durable(
                write, {"artifact_id": draft["id"], "author_kind": "user"},
            )

    await run_in_db(comment)
    for _ in range(100):
        if received:
            break
        await asyncio.sleep(0.01)

    assert _rows(db, thread_id)[draft["id"]] == ("visible", False)
    assert [(frame["type"], frame["artifact_id"], frame["affected_thread_ids"]) for frame in received] == [
        ("artifacts_changed", draft["id"], [thread_id]),
    ]


def _node(db, thread_id: str, title: str):
    plan = plans.ensure_plan("thread", thread_id, db=db)
    node = plans.add_node(
        plan.id, kind="task", title=title, acceptance_criteria="Done", db=db,
    )
    return plans.transition_node(node.id, "active", db=db)


def test_the_one_time_pass_settles_rows_finished_tasks_left_behind(db):
    from infra.settings import get_setting
    from registry import finished_task_row_backfill as backfill

    thread_id = make_session(db, prefix="rule2-pass")
    finished = _node(db, thread_id, "Finished")
    note = _bound(db, thread_id, finished, f"{_PROJECT}/.threads/t1/notes.md")
    kept = _bound(db, thread_id, finished, f"{_PROJECT}/.threads/t1/pinned.md")
    shared = _bound(db, thread_id, finished, f"{_PROJECT}/shared/documents/REFERENCE.md")
    pinning.update_pinned(kept["id"], pinned=True, parent_thread_id=thread_id, db=db)
    # The task finished while its thread-folder rows were still exempt, so
    # settlement skipped them: the node is terminal and nothing was stamped.
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (finished.id,))
    db.execute("DELETE FROM active_turns WHERE thread_id=?", (thread_id,))
    db.commit()
    open_node = _node(db, thread_id, "Open")
    in_work = _bound(db, thread_id, open_node, f"{_PROJECT}/.threads/t1/current.md")
    taskless = models.register_artifact(
        integration="workspace", type="document",
        external_id=f"{_PROJECT}/.threads/t1/taskless.md",
        path=str(_location(f"{_PROJECT}/.threads/t1/taskless.md")), parent_thread_id=thread_id,
        title="taskless.md", retention="durable", metadata={"zone": "workspace"}, db=db,
    )
    before = _rows(db, thread_id)
    assert set(before.values()) == {("visible", False)}

    backfill.run_startup_backfill()

    rows = _rows(get_db(), thread_id)
    assert rows[note["id"]] == ("settled", True)
    assert rows[kept["id"]] == ("visible", True)
    for untouched in (shared, in_work, taskless):
        assert rows[untouched["id"]] == ("visible", False)
    marker = get_setting(backfill._MARKER, db=get_db())
    assert marker["retired_rows"] == 2
    assert marker["retired_by_thread"] == {thread_id: 2}
    event = get_db().execute(
        "SELECT event_type, metadata FROM artifact_registration_events "
        "WHERE artifact_id=? ORDER BY rowid DESC LIMIT 1",
        (note["id"],),
    ).fetchone()
    assert event["event_type"] == "origin_node_retired"
    assert "finished_task_backfill" in event["metadata"]

    assert backfill.retire_finished_task_rows(get_db()) == {}
