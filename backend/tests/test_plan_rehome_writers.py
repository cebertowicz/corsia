"""Every writer of a thread's project or spawner re-derives its plan parents."""

from __future__ import annotations

import pytest


def _seed() -> dict[str, str]:
    """Owner and its subagent in project 'old', both with plans."""
    from infra.database import get_db
    from plans import service

    db = get_db()
    try:
        for project_id in ("old", "new"):
            db.execute(
                "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,'t','t')",
                (project_id, project_id),
            )
        for thread_id, spawned_by in (("owner", None), ("child", "owner")):
            db.execute(
                """INSERT INTO threads
                   (id,user_id,title,project_id,spawned_by,model,created_at,updated_at)
                   VALUES (?,'user',?,'old',?,'native/test','t','t')""",
                (thread_id, thread_id, spawned_by),
            )
        db.commit()
        plans = {
            owner: service.ensure_plan("thread", owner, db=db).id
            for owner in ("owner", "child")
        }
        for project_id in ("old", "new"):
            plans[project_id] = service.ensure_plan("project_scope", project_id, db=db).id
        db.commit()
        return plans
    finally:
        db.close()


def _parents() -> dict[str, str | None]:
    from infra.database import get_db

    db = get_db()
    try:
        return {
            row["owner_id"]: row["parent_plan_id"]
            for row in db.execute(
                "SELECT owner_id,parent_plan_id FROM plans WHERE owner_kind='thread'"
            )
        }
    finally:
        db.close()


def test_session_project_move_rehomes_the_plan_and_its_subagents(isolated_db):
    from api.thread_mutations import _write_metadata_patch

    plans = _seed()

    assert _write_metadata_patch("owner", "user", {"project_id": "new"}, "t1")["status"] == "ok"

    assert _parents() == {"owner": plans["new"], "child": plans["old"]}


@pytest.mark.asyncio
async def test_conversation_set_project_rehomes_the_plan_and_its_subagents(
    isolated_db, monkeypatch,
):
    from tools.query_projects import SetConversationProjectTool

    plans = _seed()

    async def _no_boundary(*_args, **_kwargs):
        return None

    monkeypatch.setattr("thread.runtime_boundary.post_context_boundary_effects", _no_boundary)
    await SetConversationProjectTool().execute("owner", "new")

    assert _parents() == {"owner": plans["new"], "child": plans["old"]}


def _delete_owner() -> None:
    from api.thread_deletion import delete_thread_rows, family_versions
    from core.thread_family import thread_family_ids
    from infra.database import get_db

    db = get_db()
    try:
        family = thread_family_ids(db, "owner")
    finally:
        db.close()
    assert delete_thread_rows("owner", "user", family_versions(family)) == "ok"


def test_deleting_a_spawner_deletes_its_subagent_and_leaves_that_plan_to_plans(isolated_db):
    # The subagent goes with its spawner (#2647); retiring the plan rows a
    # deleted owner leaves is Structured Plans' change on the same issue.
    plans = _seed()

    _delete_owner()

    assert _parents() == {"owner": plans["old"], "child": plans["owner"]}


def test_deleting_a_spawner_rehomes_its_promoted_agents_plans(isolated_db):
    from infra.database import get_db

    plans = _seed()
    db = get_db()
    try:
        db.execute("UPDATE threads SET agent_enabled=1 WHERE id='child'")
        db.commit()
    finally:
        db.close()

    _delete_owner()

    assert _parents() == {"owner": plans["old"], "child": plans["old"]}
