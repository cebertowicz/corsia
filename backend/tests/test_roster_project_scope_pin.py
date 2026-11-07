"""Regression pin: workspace roster surfaces keep project agents out of scope."""

import pytest

from core.agent_context_sources import AgentRosterTool, current_responsibility_map_snapshot
from infra.corsia_agent import set_corsia_agent
from infra.database import get_db
from tests.registry_helpers import make_session
from tools.agent_roster_update import AgentRosterEntryTool


@pytest.mark.asyncio
async def test_workspace_roster_and_map_exclude_local_only_project_agent(isolated_db):
    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('private','Private','t','t')")
        db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('private',1)")
        workspace = make_session(db, prefix="workspace")
        project = make_session(db, project_id="private", prefix="private-agent")
        db.execute("UPDATE threads SET model='cloud/example',local_only=0 WHERE id=?", (workspace,))
        db.execute(
            "UPDATE threads SET agent_enabled=1,agent_status='active',agent_responsibility=?,"
            "agent_domains=?,agent_notes=? WHERE id=?",
            ('private charter sentinel', '[\"private domain sentinel\"]', 'private notes sentinel', project),
        )
        assert set_corsia_agent(workspace, db=db)
        db.commit()
        snapshot = current_responsibility_map_snapshot(workspace, db, model='cloud/example')
    finally:
        db.close()

    roster = await AgentRosterTool().execute(_thread_id=workspace)
    entry = await AgentRosterEntryTool().execute(project, _thread_id=workspace)
    for value in ('private charter sentinel', 'private domain sentinel', 'private notes sentinel'):
        assert value not in roster
        assert value not in snapshot['content']
        assert value not in entry
    assert 'must share a scope' in entry
