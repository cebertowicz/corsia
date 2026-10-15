"""The CORSIA agent's capability set against a project conductor and an ordinary thread.

The workspace scope's occupant holds every conductor capability plus the
workspace-only operations (bulletin dispositions, workspace ownership
authority, workspace roster edits). A project's conductor holds the
scope-generic capabilities and is refused the workspace-only ones.
"""
from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

import pytest

from infra.conductor import WORKSPACE_SCOPE, set_scope_coordinator
from infra.database import get_db
from tests.registry_helpers import make_session


@pytest.fixture
def actors(isolated_db):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) "
            "VALUES ('alpha','Alpha','t','t')"
        )
        db.commit()
        agent = make_session(db, prefix="agent")
        conductor = make_session(db, prefix="conductor", project_id="alpha")
        ordinary = make_session(db, prefix="ordinary")
        workspace_target = make_session(db, prefix="ws-target")
        project_target = make_session(db, prefix="p-target", project_id="alpha")
        assert set_corsia_agent(agent, db=db)
        assert set_project_conductor("alpha", conductor, db=db)
        return {
            "agent": agent,
            "conductor": conductor,
            "ordinary": ordinary,
            "workspace_target": workspace_target,
            "project_target": project_target,
        }
    finally:
        db.close()


def test_roles_follow_the_scope_each_actor_occupies(actors):
    from core.role_context_sources import infer_thread_roles

    db = get_db()
    try:
        roles = {
            name: infer_thread_roles(thread_id, db=db)
            for name, thread_id in actors.items()
        }
    finally:
        db.close()
    assert roles["agent"] == ("agent", "corsia_agent")
    assert roles["conductor"] == ("agent", "conductor")
    assert "corsia_agent" not in roles["conductor"]
    assert "conductor" not in roles["agent"]
    assert "corsia_agent" not in roles["ordinary"]
    assert "conductor" not in roles["ordinary"]


def test_scope_generic_tools_admit_both_and_refuse_an_ordinary_thread(actors):
    from tools.corsia_api import ApiCallError, conductor_user
    from tools.log_read import LogReadError, _require_conductor

    for name in ("agent", "conductor"):
        assert conductor_user(actors[name]) == "steve"
        _require_conductor(actors[name])
    with pytest.raises(ApiCallError):
        conductor_user(actors["ordinary"])
    with pytest.raises(LogReadError):
        _require_conductor(actors["ordinary"])


def test_bulletin_dispositions_are_the_corsia_agents_alone(actors):
    from tools.bulletins import _record_disposition

    for name in ("conductor", "ordinary"):
        message, source = _record_disposition("anything", "acted", "x", actors[name])
        assert message.startswith("Error: Only the CORSIA agent records bulletin dispositions")
        assert source is None
    message, _source = _record_disposition("unknown-id", "acted", "x", actors["agent"])
    assert message.startswith("Error: Unknown bulletin id")


def test_workspace_ownership_authority_is_the_corsia_agents_alone(actors):
    from registry.ownership_authority import can_rehome, can_release

    db = get_db()
    try:
        workspace_row = db.execute(
            "SELECT * FROM threads WHERE id=?", (actors["workspace_target"],)
        ).fetchone()
        for authority in ("workspace-corsia", "blessed-edge-policy"):
            kwargs = dict(
                authority=authority,
                current_owner_thread_id=None,
                source_thread_row=workspace_row,
                target_thread_row=workspace_row,
                db=db,
            )
            assert can_rehome(actor_thread_id=actors["agent"], **kwargs)
            assert not can_rehome(actor_thread_id=actors["conductor"], **kwargs)
            assert not can_rehome(actor_thread_id=actors["ordinary"], **kwargs)
            assert can_release(
                authority=authority,
                actor_thread_id=actors["agent"],
                current_owner_thread_id="owner",
                source_thread_row=workspace_row,
                triage_thread_row=None,
                db=db,
            )
            assert not can_release(
                authority=authority,
                actor_thread_id=actors["conductor"],
                current_owner_thread_id="owner",
                source_thread_row=workspace_row,
                triage_thread_row=None,
                db=db,
            )
    finally:
        db.close()


def test_roster_edits_stay_inside_the_scope_each_designation_owns(actors):
    from core.agent_roster_mutations import (
        RosterChangeError,
        require_designated_conductor_target,
    )

    db = get_db()
    try:
        def reach(actor: str, target: str):
            return require_designated_conductor_target(
                actors[target], user_id="steve", actor_thread_id=actors[actor], db=db,
            )

        assert reach("agent", "workspace_target")["id"] == actors["workspace_target"]
        assert reach("conductor", "project_target")["id"] == actors["project_target"]
        # The project conductor is refused the workspace's roster, the CORSIA
        # agent a project's, and an ordinary thread either.
        for actor, target in (
            ("conductor", "workspace_target"),
            ("agent", "project_target"),
            ("ordinary", "workspace_target"),
        ):
            with pytest.raises(RosterChangeError):
                reach(actor, target)
    finally:
        db.close()
