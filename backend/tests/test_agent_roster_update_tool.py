"""A trusted conductor can edit only its own roster scope."""

from __future__ import annotations

import json

import pytest

from infra.conductor import set_conductor
from infra.database import get_db
from tests.registry_helpers import make_session
from tools.agent_roster_update import AgentRosterEntryTool, AgentRosterUpdateTool


def _project_and_threads():
    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            ("roster-project", "Roster project", "t", "t"),
        )
        db.commit()
        conductor = make_session(db, project_id="roster-project", prefix="conductor")
        target = make_session(db, project_id="roster-project", prefix="target")
        ordinary = make_session(db, project_id="roster-project", prefix="ordinary")
        foreign = make_session(db, prefix="workspace-target")
        assert set_conductor("roster-project", conductor, db=db)
        return conductor, target, ordinary, foreign
    finally:
        db.close()


def _roster_change_count() -> int:
    db = get_db()
    try:
        return db.execute(
            "SELECT COUNT(*) FROM context_change_journal "
            "WHERE object_kind='agent_roster'"
        ).fetchone()[0]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_conductor_roster_update_is_revisioned_and_idempotent(isolated_db):
    conductor, target, _, _ = _project_and_threads()
    entry = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))
    assert entry["thread_id"] == target
    assert entry["agent_enabled"] is False

    changed = json.loads(await AgentRosterUpdateTool().execute(
        target, entry["updated_at"], responsibility="Owns project routing.",
        _thread_id=conductor,
    ))
    assert changed == {"ok": True, "changed": True, "thread_id": target}
    assert _roster_change_count() == 1

    current = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))
    assert current["responsibility"] == "Owns project routing."
    assert current["agent_enabled"] is True
    assert current["updated_at"] != entry["updated_at"]

    retry = json.loads(await AgentRosterUpdateTool().execute(
        target, entry["updated_at"], responsibility="Owns project routing.",
        _thread_id=conductor,
    ))
    assert retry["changed"] is False
    assert _roster_change_count() == 1

    stale = await AgentRosterUpdateTool().execute(
        target, entry["updated_at"], responsibility="Owns another area.",
        _thread_id=conductor,
    )
    assert "Roster entry changed" in stale
    assert _roster_change_count() == 1


@pytest.mark.asyncio
async def test_demote_then_reenable_preserves_omitted_responsibility(isolated_db):
    conductor, target, _, _ = _project_and_threads()
    initial = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))
    promoted = json.loads(await AgentRosterUpdateTool().execute(
        target, initial["updated_at"], responsibility="Owns project routing.",
        _thread_id=conductor,
    ))
    assert promoted["changed"] is True

    active_entry = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))
    demoted = json.loads(await AgentRosterUpdateTool().execute(
        target, active_entry["updated_at"], agent_enabled=False, _thread_id=conductor,
    ))
    assert demoted["changed"] is True
    inactive_entry = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))
    assert inactive_entry["responsibility"] == "Owns project routing."

    reenabled = json.loads(await AgentRosterUpdateTool().execute(
        target, inactive_entry["updated_at"], agent_enabled=True, _thread_id=conductor,
    ))
    assert reenabled["changed"] is True
    restored = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))
    assert restored["responsibility"] == "Owns project routing."
    assert restored["agent_enabled"] is True
    assert _roster_change_count() == 3


@pytest.mark.asyncio
async def test_editing_a_demoted_agents_responsibility_keeps_it_demoted(isolated_db):
    conductor, target, _, _ = _project_and_threads()
    initial = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))
    await AgentRosterUpdateTool().execute(
        target, initial["updated_at"], responsibility="Owns project routing.",
        _thread_id=conductor,
    )
    active_entry = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))
    await AgentRosterUpdateTool().execute(
        target, active_entry["updated_at"], agent_enabled=False, _thread_id=conductor,
    )
    inactive_entry = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))

    edited = json.loads(await AgentRosterUpdateTool().execute(
        target, inactive_entry["updated_at"], responsibility="Owns release routing.",
        _thread_id=conductor,
    ))

    assert edited["changed"] is True
    after = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))
    assert after["responsibility"] == "Owns release routing."
    assert after["agent_enabled"] is False
    assert after["active"] is False


@pytest.mark.asyncio
async def test_demoting_a_legacy_demoted_row_is_a_no_op(isolated_db):
    conductor, target, _, _ = _project_and_threads()
    db = get_db()
    try:
        # The shape the old demote left behind: responsibility wiped to NULL.
        db.execute(
            "UPDATE threads SET agent_enabled=0, agent_status='inactive', "
            "agent_responsibility=NULL, agent_paths='[]', agent_domains='[]' WHERE id=?",
            (target,),
        )
        db.commit()
    finally:
        db.close()
    entry = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))

    result = json.loads(await AgentRosterUpdateTool().execute(
        target, entry["updated_at"], agent_enabled=False, _thread_id=conductor,
    ))

    assert result["changed"] is False
    after = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))
    assert after["updated_at"] == entry["updated_at"]
    assert _roster_change_count() == 0


@pytest.mark.asyncio
async def test_roster_revision_ignores_unrelated_thread_writes(isolated_db):
    conductor, target, _, _ = _project_and_threads()
    entry = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))

    db = get_db()
    try:
        db.execute("UPDATE threads SET title='Retitled', updated_at='after-read' WHERE id=?", (target,))
        db.commit()
    finally:
        db.close()

    changed = json.loads(await AgentRosterUpdateTool().execute(
        target, entry["updated_at"], responsibility="Owns project routing.", _thread_id=conductor,
    ))
    assert changed == {"ok": True, "changed": True, "thread_id": target}


@pytest.mark.asyncio
async def test_roster_tools_deny_ordinary_and_foreign_scope_threads(isolated_db):
    conductor, target, ordinary, foreign = _project_and_threads()
    entry = json.loads(await AgentRosterEntryTool().execute(target, _thread_id=conductor))

    ordinary_read = await AgentRosterEntryTool().execute(target, _thread_id=ordinary)
    ordinary_write = await AgentRosterUpdateTool().execute(
        target, entry["updated_at"], responsibility="Unauthorized.",
        _thread_id=ordinary,
    )
    foreign_read = await AgentRosterEntryTool().execute(foreign, _thread_id=conductor)
    foreign_write = await AgentRosterUpdateTool().execute(
        foreign, entry["updated_at"], responsibility="Unauthorized.",
        _thread_id=conductor,
    )

    assert "not the designated conductor" in ordinary_read
    assert "not the designated conductor" in ordinary_write
    assert "must share a scope" in foreign_read
    assert "must share a scope" in foreign_write
    assert _roster_change_count() == 0
