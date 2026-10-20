"""Responsibility-map changes use the existing passive journal."""

from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

import json

import pytest


def _insert_project(project_id: str) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            (project_id, project_id, "t", "t"),
        )
        db.commit()
    finally:
        db.close()


def _insert_thread(
    thread_id: str,
    *,
    project_id: str | None = None,
    title: str = "Owner",
    enabled: bool = True,
    status: str = "active",
    responsibility: str = "Owns releases.",
) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,title,project_id,model,agent_enabled,agent_status,"
            "agent_responsibility,created_at,updated_at) "
            "VALUES (?, 'user', ?, ?, 'native/test', ?, ?, ?, 't', 't')",
            (
                thread_id,
                title,
                project_id,
                1 if enabled else 0,
                status,
                responsibility,
            ),
        )
        db.commit()
    finally:
        db.close()


def _roster_changes() -> list[dict]:
    from infra.database import get_db

    db = get_db()
    try:
        return [
            dict(row)
            for row in db.execute(
                "SELECT object_id,change_kind,summary,details_json "
                "FROM context_change_journal WHERE object_kind='agent_roster' "
                "ORDER BY seq"
            ).fetchall()
        ]
    finally:
        db.close()


def test_passive_roster_delivery_subscribes_to_only_the_threads_exact_scope(
    isolated_db, monkeypatch,
):
    from infra.database import get_db
    from infra.passive_changes import append_change, materialize_pending_digest

    _insert_project("project")
    _insert_thread("project-thread", project_id="project", enabled=False)
    _insert_thread("workspace-thread", enabled=False)
    _insert_thread("project-owner", project_id="project", title="Project Owner")
    _insert_thread("workspace-owner", title="Workspace Owner")
    db = get_db()
    try:
        monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
        append_change(
            db,
            object_kind="agent_roster",
            object_id="user:user:workspace",
            change_kind="updated",
            summary="workspace owner changed",
        )
        append_change(
            db,
            object_kind="agent_roster",
            object_id="user:user:project:project",
            change_kind="updated",
            summary="project owner changed",
        )
        db.commit()

        project_digest = materialize_pending_digest(db, "project-thread")
        workspace_digest = materialize_pending_digest(db, "workspace-thread")

        assert project_digest is not None
        assert workspace_digest is not None
        project_metadata = json.loads(
            db.execute(
                "SELECT metadata FROM messages WHERE id=?",
                (project_digest.message_id,),
            ).fetchone()["metadata"]
        )
        workspace_metadata = json.loads(
            db.execute(
                "SELECT metadata FROM messages WHERE id=?",
                (workspace_digest.message_id,),
            ).fetchone()["metadata"]
        )
        project_map = project_metadata["context_events"][0]["summary"]
        workspace_map = workspace_metadata["context_events"][0]["summary"]
        assert project_map.count("[Agent responsibility map]") == 1
        assert "Project Owner" in project_map
        assert "Workspace Owner" not in project_map
        assert workspace_map.count("[Agent responsibility map]") == 1
        assert "Workspace Owner" in workspace_map
        assert "Project Owner" not in workspace_map
    finally:
        db.close()


def test_multiple_roster_changes_materialize_one_complete_current_map(
    isolated_db, monkeypatch,
):
    from api.thread_mutations import _write_metadata_patch
    from infra.database import get_db
    from infra.passive_changes import materialize_pending_digest

    _insert_project("project")
    _insert_thread("thread", project_id="project", enabled=False)
    _insert_thread("owner", project_id="project", title="Original Owner")
    assert _write_metadata_patch(
        "owner", "user", {"title": "Former Owner"}, "t1"
    )["status"] == "ok"
    assert _write_metadata_patch(
        "owner", "user", {"title": "Release Owner"}, "t2"
    )["status"] == "ok"

    db = get_db()
    try:
        monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
        accepted = materialize_pending_digest(db, "thread")

        assert accepted is not None
        row = db.execute(
            "SELECT content,metadata FROM messages WHERE id=?", (accepted.message_id,),
        ).fetchone()
        expected_map = "\n".join(
            [
                "[Agent responsibility map]",
                "Self: this thread (thread) has no registered responsibility.",
                "Other agents in scope:",
                "- Release Owner (owner) [project] — Owns releases.",
                "CORSIA agent: not reachable (none designated).",
                "Use agent_roster for paths, domains, and coordination notes.",
            ]
        )
        metadata = json.loads(row["metadata"])
        roster_events = [
            event
            for event in metadata["context_events"]
            if event.get("object_kind") == "agent_roster"
        ]
        assert row["content"] == f"[PASSIVE CONTEXT DIGEST]\n{expected_map}"
        assert [event["summary"] for event in roster_events] == [expected_map]
        assert "Former Owner" not in row["content"]
        assert [change["summary"] for change in _roster_changes()] == [
            "Agent responsibility map updated: Former Owner",
            "Agent responsibility map updated: Release Owner",
        ]
    finally:
        db.close()


def test_roster_map_survives_the_passive_detail_bound(isolated_db, monkeypatch):
    from infra.database import get_db
    from infra.passive_changes import append_change, materialize_pending_digest

    _insert_project("project")
    _insert_thread("thread", project_id="project", enabled=False)
    _insert_thread("owner", project_id="project", title="Bounded Owner")
    db = get_db()
    try:
        monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
        append_change(
            db,
            object_kind="agent_roster",
            object_id="user:user:project:project",
            change_kind="updated",
            summary="lightweight roster mutation",
        )
        for index in range(101):
            append_change(
                db,
                object_kind="global_context",
                object_id=f"global-{index}",
                change_kind="updated",
                summary=f"global context {index}",
            )

        accepted = materialize_pending_digest(db, "thread")

        assert accepted is not None
        metadata = json.loads(
            db.execute(
                "SELECT metadata FROM messages WHERE id=?", (accepted.message_id,),
            ).fetchone()["metadata"]
        )
        roster_events = [
            event
            for event in metadata["context_events"]
            if event.get("object_kind") == "agent_roster"
        ]
        assert metadata["detail_truncated"] is True
        assert len(metadata["context_events"]) == 100
        assert len(roster_events) == 1
        assert roster_events[0]["summary"].count("[Agent responsibility map]") == 1
        assert "Bounded Owner" in roster_events[0]["summary"]
    finally:
        db.close()


def test_session_title_change_journals_only_a_visible_owner(isolated_db):
    from api.thread_mutations import _write_metadata_patch

    _insert_thread("visible")
    _insert_thread("inactive", status="inactive")

    assert _write_metadata_patch(
        "visible", "user", {"title": "Visible renamed"}, "t1"
    )["status"] == "ok"
    assert _write_metadata_patch(
        "inactive", "user", {"title": "Inactive renamed"}, "t1"
    )["status"] == "ok"

    changes = _roster_changes()
    assert [change["object_id"] for change in changes] == ["user:user:workspace"]
    assert changes[0]["summary"] == "Agent responsibility map updated: Visible renamed"


def test_unrelated_metadata_write_survives_corrupt_workspace_conductor_setting(
    isolated_db,
):
    from api.thread_mutations import _write_metadata_patch
    from infra.conductor import get_scope_coordinator
    from infra.corsia_agent import CorsiaAgentSettingError
    from infra.database import get_db

    _insert_thread("owner")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
            ("corsia_agent_thread_id", "{not-json", "t"),
        )
        db.commit()
    finally:
        db.close()

    assert _write_metadata_patch(
        "owner", "user", {"title": "Renamed despite corrupt setting"}, "t1"
    )["status"] == "ok"

    db = get_db()
    try:
        assert db.execute(
            "SELECT title FROM threads WHERE id='owner'"
        ).fetchone()["title"] == "Renamed despite corrupt setting"
    finally:
        db.close()
    assert [change["object_id"] for change in _roster_changes()] == [
        "user:user:workspace"
    ]
    with pytest.raises(CorsiaAgentSettingError, match="CORSIA agent"):
        get_corsia_agent()


def test_project_metadata_write_skips_workspace_conductor_lookup(
    isolated_db, monkeypatch,
):
    from api.thread_mutations import _write_metadata_patch
    from infra.database import get_db

    _insert_project("project")
    _insert_thread("owner", project_id="project")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
            ("corsia_agent_thread_id", "{not-json", "t"),
        )
        db.commit()
    finally:
        db.close()

    def _unexpected_lookup(*_args, **_kwargs):
        pytest.fail("project roster writes must not read workspace conductor state")

    monkeypatch.setattr("infra.conductor.get_scope_coordinator", _unexpected_lookup)

    assert _write_metadata_patch(
        "owner", "user", {"title": "Project owner renamed"}, "t1"
    )["status"] == "ok"
    assert [change["object_id"] for change in _roster_changes()] == [
        "user:user:project:project"
    ]


def test_session_project_move_journals_old_and_new_exact_scopes(isolated_db):
    from api.thread_mutations import _write_metadata_patch

    _insert_project("old")
    _insert_project("new")
    _insert_thread("owner", project_id="old")

    result = _write_metadata_patch("owner", "user", {"project_id": "new"}, "t1")

    assert result["status"] == "ok"
    assert [change["object_id"] for change in _roster_changes()] == [
        "user:user:project:old",
        "user:user:project:new",
    ]


def test_session_archive_and_unarchive_each_journal_visible_transition(isolated_db):
    from api.thread_mutations import _write_metadata_patch

    _insert_thread("owner")

    assert _write_metadata_patch(
        "owner", "user", {"archived": True}, "t1"
    )["archive_changed"] is True
    assert _write_metadata_patch(
        "owner", "user", {"archived": False}, "t2"
    )["archive_changed"] is True

    assert [change["object_id"] for change in _roster_changes()] == [
        "user:user:workspace",
        "user:user:workspace",
    ]


def test_thread_delete_journals_removed_visible_owner(isolated_db):
    from api.thread_deletion import delete_thread_rows, family_versions

    _insert_project("project")
    _insert_thread("owner", project_id="project")

    assert delete_thread_rows("owner", "user", family_versions(["owner"])) == "ok"
    assert [change["object_id"] for change in _roster_changes()] == [
        "user:user:project:project"
    ]


@pytest.mark.asyncio
async def test_conversation_set_project_journals_both_owner_scopes(
    isolated_db, monkeypatch,
):
    from tools.query_projects import SetConversationProjectTool

    _insert_project("old")
    _insert_project("new")
    _insert_thread("owner", project_id="old")

    async def _no_boundary(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "thread.runtime_boundary.post_context_boundary_effects", _no_boundary
    )
    result = await SetConversationProjectTool().execute("owner", "new")

    assert "attached to project 'new'" in result
    assert [change["object_id"] for change in _roster_changes()] == [
        "user:user:project:old",
        "user:user:project:new",
    ]


@pytest.mark.asyncio
async def test_automatic_title_journals_only_visible_owner(isolated_db, monkeypatch):
    from infra.database import get_db
    from ws.post_turn import ensure_thread_title

    _insert_thread("owner", title="New Chat")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
            ("corsia_agent_thread_id", "{not-json", "t"),
        )
        db.commit()
    finally:
        db.close()

    async def _title(_recent, _model, *, thread_id=None):
        return "Release ownership"

    async def _send(_message):
        return None

    monkeypatch.setattr("ws.post_turn.generate_thread_title", _title)
    await ensure_thread_title("owner", _send)

    db = get_db()
    try:
        assert db.execute(
            "SELECT title FROM threads WHERE id='owner'"
        ).fetchone()["title"] == "Release ownership"
    finally:
        db.close()
    changes = _roster_changes()
    assert [change["object_id"] for change in changes] == ["user:user:workspace"]
    assert changes[0]["summary"] == "Agent responsibility map updated: Release ownership"


def _project_roster_digest(db, thread_id: str) -> str:
    from infra.passive_changes import materialize_pending_digest

    accepted = materialize_pending_digest(db, thread_id)
    assert accepted is not None
    metadata = json.loads(
        db.execute(
            "SELECT metadata FROM messages WHERE id=?", (accepted.message_id,),
        ).fetchone()["metadata"]
    )
    roster_events = [
        event
        for event in metadata["context_events"]
        if event.get("object_kind") == "agent_roster"
    ]
    assert len(roster_events) == 1
    db.commit()
    return roster_events[0]["summary"]


def test_project_digest_refreshes_when_workspace_conductor_is_designated_or_renamed(
    isolated_db, monkeypatch,
):
    from api.thread_mutations import _write_metadata_patch
    from infra.conductor import set_scope_coordinator
    from infra.database import get_db

    _insert_project("project")
    _insert_project("project-two")
    _insert_thread("project-agent", project_id="project", enabled=False)
    _insert_thread("project-agent-two", project_id="project-two", enabled=False)
    _insert_thread("contact", title="Original Contact")
    _insert_thread("next-contact", title="Next Contact")
    _insert_thread("workspace-other", title="Workspace Other")
    db = get_db()
    try:
        monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)

        assert set_corsia_agent("contact", db=db) is True
        assert [change["object_id"] for change in _roster_changes()] == [
            "user:user:corsia-agent-contact"
        ]
        designated_map = _project_roster_digest(db, "project-agent")
        second_project_map = _project_roster_digest(db, "project-agent-two")
        assert "Original Contact (contact) [workspace]" in designated_map
        assert "Original Contact (contact) [workspace]" in second_project_map
        assert "Workspace Other" not in designated_map

        assert _write_metadata_patch(
            "contact", "user", {"title": "Renamed Contact"}, "t1",
        )["status"] == "ok"
        renamed_map = _project_roster_digest(db, "project-agent")
        assert "Renamed Contact (contact) [workspace]" in renamed_map
        assert "Original Contact" not in renamed_map
        assert "Workspace Other" not in renamed_map

        assert set_corsia_agent("next-contact", db=db) is True
        switched_map = _project_roster_digest(db, "project-agent")
        assert "Next Contact (next-contact) [workspace]" in switched_map
        assert "Renamed Contact" not in switched_map
        assert "Workspace Other" not in switched_map

        assert set_corsia_agent(None, db=db) is True
        cleared_map = _project_roster_digest(db, "project-agent-two")
        assert "Next Contact" not in cleared_map
        assert "Workspace Other" not in cleared_map
    finally:
        db.close()


async def test_project_digest_refreshes_when_workspace_contact_responsibility_changes(
    isolated_db, monkeypatch,
):
    from api.agents import update_agent
    from infra.conductor import set_scope_coordinator
    from infra.database import get_db

    _insert_project("project")
    _insert_thread("project-agent", project_id="project", enabled=False)
    _insert_thread("contact", title="Workspace Contact")
    db = get_db()
    try:
        monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
        assert set_corsia_agent("contact", db=db) is True
        _project_roster_digest(db, "project-agent")

        assert await update_agent(
            "contact", {"responsibility": "Coordinates all workspaces."}, {"id": "user"},
        ) == {"ok": True}
        updated_map = _project_roster_digest(db, "project-agent")
        assert "Coordinates all workspaces." in updated_map
        assert "Owns releases." not in updated_map
    finally:
        db.close()


def test_project_digest_refreshes_when_workspace_contact_is_archived(
    isolated_db, monkeypatch,
):
    from api.thread_mutations import _write_metadata_patch
    from infra.conductor import set_scope_coordinator
    from infra.database import get_db

    _insert_project("project")
    _insert_thread("project-agent", project_id="project", enabled=False)
    _insert_thread("contact", title="Workspace Contact")
    _insert_thread("workspace-other", title="Workspace Other")
    db = get_db()
    try:
        monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
        assert set_corsia_agent("contact", db=db) is True
        assert "Workspace Contact (contact) [workspace]" in _project_roster_digest(
            db, "project-agent",
        )

        assert _write_metadata_patch(
            "contact", "user", {"archived": True}, "t1",
        )["status"] == "ok"
        archived_map = _project_roster_digest(db, "project-agent")
        assert "Workspace Contact" not in archived_map
        assert "Workspace Other" not in archived_map
    finally:
        db.close()
