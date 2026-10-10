from __future__ import annotations

import uuid

import pytest
from auth_fixtures import OWNER_TOKEN

_TOKEN = OWNER_TOKEN


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.mark.integration
class TestAgentsApi:
    def _session(self, client, *, title: str, project_id: str | None = None) -> str:
        body = {"title": title}
        if project_id:
            body["project_id"] = project_id
        resp = client.post(f"/api/threads?token={_TOKEN}", json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()["thread_id"]

    def test_promote_agent_and_roster_context(self, integration_client):
        from core.dynamic_context_registry import collect_context_sources
        from infra.config import WORKSPACE
        from infra.database import get_db

        sid = self._session(integration_client, title="Release Coordinator")
        promoted = integration_client.patch(
            f"/api/agents/{sid}?token={_TOKEN}",
            json={
                "responsibility": "Coordinates releases.",
                "responsibility_paths": ["releases/"],
                "responsibility_domains": ["release coordination"],
                "coordination_notes": "Ping for release gates.",
            },
        )
        assert promoted.status_code == 200, promoted.text
        listed = integration_client.get(f"/api/agents?token={_TOKEN}")
        assert listed.status_code == 200, listed.text
        agents = listed.json()["agents"]
        assert agents[0]["thread_id"] == sid
        assert agents[0]["responsibility"] == "Coordinates releases."

        db = get_db()
        try:
            change = db.execute(
                "SELECT object_kind,object_id,summary FROM context_change_journal "
                "ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            message_count = db.execute(
                "SELECT COUNT(*) FROM messages WHERE thread_id=?", (sid,),
            ).fetchone()[0]
            collection = collect_context_sources(
                thread_id=sid,
                project_id=None,
                workspace=WORKSPACE,
                db=db,
            )
        finally:
            db.close()
        assert tuple(change) == (
            "agent_roster",
            "user:steve:workspace",
            "Agent responsibility map updated: Release Coordinator",
        )
        assert message_count == 0
        roster = next(
            source for source in collection.sources
            if source.source_id == "role:agent-owner-map"
        )
        assert roster.inclusion == "included"
        assert "Release Coordinator" in (roster.prompt_content or "")

    def test_invisible_agent_update_does_not_journal_roster_change(
        self, integration_client
    ):
        from infra.database import get_db

        sid = self._session(integration_client, title="Inactive Coordinator")
        db = get_db()
        try:
            before_seq = db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM context_change_journal"
            ).fetchone()[0]
        finally:
            db.close()
        response = integration_client.patch(
            f"/api/agents/{sid}?token={_TOKEN}",
            json={"responsibility": "Coordinates releases.", "active": False},
        )
        assert response.status_code == 200, response.text

        db = get_db()
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM context_change_journal "
                "WHERE object_kind='agent_roster' AND seq>?",
                (before_seq,),
            ).fetchone()[0]
        finally:
            db.close()
        assert count == 0

    def test_demote_agent_journals_former_exact_project_scope(
        self, integration_client
    ):
        from infra.database import get_db

        project = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": _unique("demotion-project")},
        )
        assert project.status_code == 200, project.text
        project_id = project.json()["project"]["id"]
        sid = self._session(
            integration_client,
            title="Project Coordinator",
            project_id=project_id,
        )
        db = get_db()
        try:
            before_seq = db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM context_change_journal"
            ).fetchone()[0]
        finally:
            db.close()
        promoted = integration_client.patch(
            f"/api/agents/{sid}?token={_TOKEN}",
            json={"responsibility": "Coordinates project releases."},
        )
        assert promoted.status_code == 200, promoted.text

        demoted = integration_client.delete(f"/api/agents/{sid}?token={_TOKEN}")
        assert demoted.status_code == 200, demoted.text

        db = get_db()
        try:
            changes = db.execute(
                "SELECT object_id,summary FROM context_change_journal "
                "WHERE object_kind='agent_roster' AND seq>? ORDER BY seq",
                (before_seq,),
            ).fetchall()
        finally:
            db.close()
        assert [tuple(change) for change in changes] == [
            (
                f"user:steve:project:{project_id}",
                "Agent responsibility map updated: Project Coordinator",
            ),
            (
                f"user:steve:project:{project_id}",
                "Agent responsibility map updated: Project Coordinator",
            ),
        ]

    def test_workspace_and_project_rosters_remain_isolated(self, integration_client):
        from core.dynamic_context_registry import collect_context_sources
        from infra.config import WORKSPACE
        from infra.database import get_db

        project = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": _unique("agent-roster-project")},
        )
        assert project.status_code == 200, project.text
        project_id = project.json()["project"]["id"]

        workspace_title = _unique("Workspace Agent")
        project_title = _unique("Project Agent")
        workspace_agent = self._session(integration_client, title=workspace_title)
        project_agent = self._session(
            integration_client,
            title=project_title,
            project_id=project_id,
        )
        for thread_id, responsibility in (
            (workspace_agent, "Owns workspace release coordination."),
            (project_agent, "Owns project release coordination."),
        ):
            promoted = integration_client.patch(
                f"/api/agents/{thread_id}?token={_TOKEN}",
                json={"responsibility": responsibility},
            )
            assert promoted.status_code == 200, promoted.text

        workspace_rows = integration_client.get(
            f"/api/agents?token={_TOKEN}"
        ).json()["agents"]
        project_rows = integration_client.get(
            f"/api/agents?token={_TOKEN}&project_id={project_id}"
        ).json()["agents"]
        workspace_ids = {row["thread_id"] for row in workspace_rows}
        project_ids = {row["thread_id"] for row in project_rows}
        assert workspace_agent in workspace_ids
        assert project_agent not in workspace_ids
        assert project_agent in project_ids
        assert workspace_agent not in project_ids

        db = get_db()
        try:
            workspace_collection = collect_context_sources(
                thread_id=workspace_agent,
                project_id=None,
                workspace=WORKSPACE,
                db=db,
            )
            project_collection = collect_context_sources(
                thread_id=project_agent,
                project_id=project_id,
                workspace=WORKSPACE,
                db=db,
            )
        finally:
            db.close()

        workspace_roster = next(
            source
            for source in workspace_collection.sources
            if source.source_id == "role:agent-owner-map"
        )
        project_roster = next(
            source
            for source in project_collection.sources
            if source.source_id == "role:agent-owner-map"
        )
        assert workspace_title in (workspace_roster.prompt_content or "")
        assert project_title not in (workspace_roster.prompt_content or "")
        assert project_title in (project_roster.prompt_content or "")
        assert workspace_title not in (project_roster.prompt_content or "")

    def test_ordinary_thread_without_responsibility_is_not_agent(self, integration_client):
        sid = self._session(integration_client, title="Ordinary thread")
        listed = integration_client.get(f"/api/agents?token={_TOKEN}")
        assert listed.status_code == 200, listed.text
        assert sid not in {row["thread_id"] for row in listed.json()["agents"]}

    def test_roster_keeps_dormancy_archive_but_excludes_user_archive_and_purge(
        self, integration_client
    ):
        from infra.database import get_db

        ids = {
            state: self._session(integration_client, title=f"Agent {state}")
            for state in ("dormancy", "user", "purged")
        }
        for sid in ids.values():
            response = integration_client.patch(
                f"/api/agents/{sid}?token={_TOKEN}",
                json={"responsibility": f"Owns {sid}"},
            )
            assert response.status_code == 200

        db = get_db()
        try:
            db.execute(
                "UPDATE threads SET lifecycle_state='archived', "
                "archived_reason='dormancy' WHERE id=?",
                (ids["dormancy"],),
            )
            db.execute(
                "UPDATE threads SET lifecycle_state='archived', "
                "archived_reason='user' WHERE id=?",
                (ids["user"],),
            )
            db.execute(
                "UPDATE threads SET lifecycle_state='purged' WHERE id=?",
                (ids["purged"],),
            )
            db.commit()
        finally:
            db.close()

        roster_ids = {
            row["thread_id"]
            for row in integration_client.get(
                f"/api/agents?token={_TOKEN}"
            ).json()["agents"]
        }
        assert ids["dormancy"] in roster_ids
        assert ids["user"] not in roster_ids
        assert ids["purged"] not in roster_ids

    def test_patch_retains_existing_full_replacement_contract(self, integration_client):
        sid = self._session(integration_client, title="Roster replacement")
        first = integration_client.patch(
            f"/api/agents/{sid}",
            params={"token": _TOKEN},
            json={
                "responsibility": "Owns the first area.",
                "responsibility_paths": ["first/"],
                "responsibility_domains": ["first domain"],
                "coordination_notes": "First note",
            },
        )
        assert first.status_code == 200, first.text

        replacement = integration_client.patch(
            f"/api/agents/{sid}",
            params={"token": _TOKEN},
            json={"responsibility": "Owns the second area."},
        )
        assert replacement.status_code == 200, replacement.text
        roster = integration_client.get(
            "/api/agents", params={"token": _TOKEN},
        ).json()["agents"]
        row = next(row for row in roster if row["thread_id"] == sid)
        assert row["responsibility"] == "Owns the second area."
        assert row["responsibility_paths"] == []
        assert row["responsibility_domains"] == []
        assert row["coordination_notes"] is None

        canonical_empty = integration_client.patch(
            f"/api/agents/{sid}",
            params={"token": _TOKEN},
            json={
                "responsibility": "Owns the third area.",
                "responsibility_paths": [],
                "paths": ["legacy/"],
                "responsibility_domains": [],
                "domains": ["legacy domain"],
                "coordination_notes": "",
                "notes": "Legacy note",
            },
        )
        assert canonical_empty.status_code == 200, canonical_empty.text
        roster = integration_client.get(
            "/api/agents", params={"token": _TOKEN},
        ).json()["agents"]
        row = next(row for row in roster if row["thread_id"] == sid)
        assert row["responsibility_paths"] == []
        assert row["responsibility_domains"] == []
        assert row["coordination_notes"] == ""

    def _row(self, thread_id: str) -> dict:
        from infra.database import get_db

        db = get_db()
        try:
            row = db.execute(
                "SELECT agent_enabled, agent_responsibility, agent_paths, "
                "agent_domains, agent_notes, agent_status, "
                "agent_roster_updated_at FROM threads WHERE id=?",
                (thread_id,),
            ).fetchone()
        finally:
            db.close()
        return dict(row)

    def test_delete_demote_on_never_promoted_thread_is_a_no_op(self, integration_client):
        sid = self._session(integration_client, title="Never Promoted")
        before = self._row(sid)

        demoted = integration_client.delete(f"/api/agents/{sid}?token={_TOKEN}")

        assert demoted.status_code == 200, demoted.text
        after = self._row(sid)
        assert after["agent_enabled"] == 0
        assert after["agent_status"] == "active"
        assert after["agent_responsibility"] is None
        assert after["agent_roster_updated_at"] == before["agent_roster_updated_at"]

    def test_delete_demote_keeps_responsibility_paths_and_notes(self, integration_client):
        import json as jsonlib

        sid = self._session(integration_client, title="Kept via Delete")
        promoted = integration_client.patch(
            f"/api/agents/{sid}?token={_TOKEN}",
            json={
                "responsibility": "Coordinates kept work.",
                "responsibility_paths": ["kept/"],
                "responsibility_domains": ["kept coordination"],
                "coordination_notes": "Ping before touching kept/.",
            },
        )
        assert promoted.status_code == 200, promoted.text

        demoted = integration_client.delete(f"/api/agents/{sid}?token={_TOKEN}")
        assert demoted.status_code == 200, demoted.text

        row = self._row(sid)
        assert row["agent_enabled"] == 0
        assert row["agent_status"] == "inactive"
        assert row["agent_responsibility"] == "Coordinates kept work."
        assert jsonlib.loads(row["agent_paths"]) == ["kept/"]
        assert jsonlib.loads(row["agent_domains"]) == ["kept coordination"]
        assert row["agent_notes"] == "Ping before touching kept/."

    def test_patch_demote_keeps_responsibility_paths_and_notes(self, integration_client):
        import json as jsonlib

        sid = self._session(integration_client, title="Kept via Patch")
        promoted = integration_client.patch(
            f"/api/agents/{sid}?token={_TOKEN}",
            json={
                "responsibility": "Coordinates patch demote.",
                "responsibility_paths": ["patched/"],
                "coordination_notes": "Patch demote note.",
            },
        )
        assert promoted.status_code == 200, promoted.text

        demoted = integration_client.patch(
            f"/api/agents/{sid}?token={_TOKEN}",
            json={"agent_enabled": False},
        )
        assert demoted.status_code == 200, demoted.text

        row = self._row(sid)
        assert row["agent_enabled"] == 0
        assert row["agent_status"] == "inactive"
        assert row["agent_responsibility"] == "Coordinates patch demote."
        assert jsonlib.loads(row["agent_paths"]) == ["patched/"]
        assert row["agent_notes"] == "Patch demote note."

    @pytest.mark.asyncio
    async def test_conductor_edit_after_human_demote_does_not_repromote(
        self, integration_client
    ):
        from infra.conductor import set_conductor
        from tools.agent_roster_update import AgentRosterUpdateTool
        import json as jsonlib

        sid = self._session(integration_client, title="Human Demoted Coordinator")
        conductor_id = self._session(integration_client, title="Workspace Conductor")
        assert set_conductor("workspace", conductor_id)

        promoted = integration_client.patch(
            f"/api/agents/{sid}?token={_TOKEN}",
            json={"responsibility": "Coordinates human-demoted work."},
        )
        assert promoted.status_code == 200, promoted.text
        demoted = integration_client.delete(f"/api/agents/{sid}?token={_TOKEN}")
        assert demoted.status_code == 200, demoted.text

        before = self._row(sid)
        assert before["agent_enabled"] == 0

        edited = jsonlib.loads(await AgentRosterUpdateTool().execute(
            sid, before["agent_roster_updated_at"],
            coordination_notes="Left a note after demote.",
            _thread_id=conductor_id,
        ))
        assert edited["changed"] is True

        after = self._row(sid)
        assert after["agent_enabled"] == 0
        assert after["agent_responsibility"] == "Coordinates human-demoted work."
        assert after["agent_notes"] == "Left a note after demote."
