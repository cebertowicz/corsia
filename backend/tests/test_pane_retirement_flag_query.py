"""The pane flags only the rows it renders, by the one retirement exemption."""

from __future__ import annotations

import pytest

from infra import config
from infra.database import get_db
from infra.platform_paths import WorkspaceSelection
from registry import models, view
from tests.registry_helpers import make_session


class _NoCloseConnection:
    """Let the production pane reader use the fixture connection unchanged."""

    def __init__(self, connection):
        self._connection = connection

    def close(self) -> None:
        pass

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.fixture()
def db(isolated_db, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "shared").mkdir(parents=True)
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(
        config,
        "WORKSPACE_SELECTION",
        WorkspaceSelection(workspace_id="pane-query", root=workspace, name="Pane query"),
    )
    return get_db()


def _document(db, thread_id: str, name: str, *, shared: bool = False) -> dict:
    folder = "shared" if shared else f".threads/{thread_id}/working"
    path = config.WORKSPACE / folder / f"{name}.md"
    return models.register_artifact(
        integration="workspace",
        type="document",
        external_id=f"workspace://pane-query/{folder}/{name}.md",
        path=str(path),
        parent_thread_id=thread_id,
        title=f"{name}.md",
        retention="durable",
        metadata={"zone": "workspace"},
        db=db,
    )


def _pull_request(db, thread_id: str, name: str, state: str) -> dict:
    return models.register_artifact(
        integration="github",
        type="pull-request",
        external_id=f"o/r#{name}",
        parent_thread_id=thread_id,
        title=f"PR {name}",
        metadata={"pr_state": state},
        db=db,
    )


def test_pane_flags_visible_rows_by_the_retirement_exemption(db, monkeypatch):
    thread_id = make_session(db, prefix="pane-flag-query")
    shared = _document(db, thread_id, "shared", shared=True)
    ordinary = _document(db, thread_id, "ordinary")
    pinned_retired = _document(db, thread_id, "pinned-retired")
    open_pr = _pull_request(db, thread_id, "1", "open")
    ended_pr = _pull_request(db, thread_id, "2", "merged")
    non_pane = models.register_artifact(
        integration="codex",
        type="thread",
        external_id="thread-1",
        parent_thread_id=thread_id,
        title="Thread",
        db=db,
    )
    hidden = [_document(db, thread_id, f"hidden-{index}") for index in range(24)]
    db.execute(
        """UPDATE artifact_registrations
           SET relationship_state='owner-pinned', retired_at='past'
           WHERE artifact_id=?""",
        (pinned_retired["id"],),
    )
    db.executemany(
        "UPDATE artifact_registrations SET surfacing='settled' WHERE artifact_id=?",
        [(artifact["id"],) for artifact in hidden],
    )
    db.commit()

    monkeypatch.setattr(view, "get_db", lambda: _NoCloseConnection(db))

    pane = view.pane_for_thread(thread_id)
    visible = {
        node["id"]: node["retirement_exempt"]
        for node in pane["nodes"]
        if node["kind"] == "artifact"
    }

    # A shared document leaves with its task like any other file.
    assert visible == {
        shared["id"]: False,
        ordinary["id"]: False,
        pinned_retired["id"]: False,
        open_pr["id"]: True,
    }
    assert ended_pr["id"] not in visible
    assert non_pane["id"] not in visible
    assert not any(artifact["id"] in visible for artifact in hidden)
