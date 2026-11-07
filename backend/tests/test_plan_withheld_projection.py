"""R14: fenced plan text never reaches the receiving thread."""

import json

import pytest

from infra.project_fence import WITHHELD_LOCAL_ONLY
from plans import service
from plans.withheld_projection import for_thread, project
from tests.test_plan_read_other_thread import _reader_tool, _threads, db  # noqa: F401


def _cloud_reader(db):
    db.execute(
        "INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES ('corsia_agent_thread_id',?,'t')",
        (json.dumps({"session_id": "reader"}),),
    )
    db.execute("INSERT INTO project_inference_policies(project_id,cloud_models_off) VALUES ('project-b',1)")
    db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["status", "full", "node"])
async def test_foreign_plan_read_withholds_text(db, monkeypatch, mode):
    _threads(db, owner_project="project-b")
    _cloud_reader(db)
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(plan.id, kind="task", title="Private title", description="Private body",
                            acceptance_criteria="Private criteria", db=db)
    tools, _ = _reader_tool(db, monkeypatch)
    args = {"thread_id": "owner"}
    if mode == "full":
        args["mode"] = "full"
    if mode == "node":
        args["node_id"] = node.id
    result = json.loads(await tools.PlanReadTool().execute(**args))
    serialized = json.dumps(result)
    assert "Private" not in serialized
    assert WITHHELD_LOCAL_ONLY in serialized
    assert node.id in serialized
    if mode == "node":
        assert result["title"] == result["description"] == result["acceptance_criteria"] == WITHHELD_LOCAL_ONLY
        assert result["state"] == "pending"
        assert result["created_at"]
    elif mode == "full":
        assert result["nodes"][0]["id"] == node.id
        assert result["nodes"][0]["kind"] == "task"
    else:
        assert result["current"]["rows"][0]["id"] == node.id
        assert result["progress"]["total"] == 1


@pytest.mark.asyncio
async def test_same_project_read_is_unchanged(db, monkeypatch):
    _threads(db)
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(plan.id, kind="task", title="Visible", acceptance_criteria="Done", db=db)
    tools, _ = _reader_tool(db, monkeypatch)
    result = json.loads(await tools.PlanReadTool().execute(thread_id="owner", node_id=node.id))
    assert result["title"] == "Visible"
    assert result["acceptance_criteria"] == "Done"


@pytest.mark.asyncio
async def test_fenced_status_read_pages_with_its_cursor(db, monkeypatch):
    _threads(db, owner_project="project-b")
    _cloud_reader(db)
    plan = service.ensure_plan("thread", "owner", db=db)
    nodes = [service.add_node(plan.id, kind="task", title=f"Private {i}", acceptance_criteria="Done", db=db)
             for i in range(6)]
    tools, _ = _reader_tool(db, monkeypatch)
    first = json.loads(await tools.PlanReadTool().execute(thread_id="owner"))
    page = first["current"]
    assert page["omitted_count"] > 0
    assert page["continue_read"] == (
        f"plan_read(thread_id='owner', section='current', cursor='{page['cursor']}')"
    )
    rest = json.loads(await tools.PlanReadTool().execute(thread_id="owner", section="current", cursor=page["cursor"]))
    served = [row["id"] for row in page["rows"]] + [row["id"] for row in rest["rows"]]
    assert sorted(served) == sorted(node.id for node in nodes)
    assert "Private" not in json.dumps(rest)


@pytest.mark.asyncio
async def test_ordinary_project_text_is_not_marked_local_only(db, monkeypatch):
    _threads(db, owner_project="project-b")
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(plan.id, kind="task", title="Visible", acceptance_criteria="Done", db=db)
    assert for_thread({"title": "Visible", "id": node.id}, recipient="reader", owner="owner", db=db) == {
        "title": "Visible", "id": node.id,
    }


def test_unknown_text_and_forged_enum_are_withheld():
    assert project({"new_field": "secret", "kind": "secret", "state": "active", "item_id": "abc", "count": 2}) == {
        "new_field": WITHHELD_LOCAL_ONLY, "kind": WITHHELD_LOCAL_ONLY, "state": "active", "item_id": "abc", "count": 2,
    }


def test_unreadable_recipient_fails_closed(db):
    _threads(db, owner_project="project-b")
    _cloud_reader(db)
    assert for_thread({"title": "secret", "id": "abc"}, recipient="missing", owner="owner", db=db) == {
        "title": WITHHELD_LOCAL_ONLY, "id": "abc",
    }
    assert for_thread("visible", recipient=None, owner="owner", db=db) == "visible"


def test_own_plan_text_reads_no_caller_facts(db, monkeypatch):
    _threads(db, owner_project="project-b")
    monkeypatch.setattr("infra.corsia_agent.get_corsia_agent", lambda **_kwargs: pytest.fail("caller read"))
    assert for_thread({"title": "mine"}, recipient="owner", owner="owner", db=db) == {"title": "mine"}
