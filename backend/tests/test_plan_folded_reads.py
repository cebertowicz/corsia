"""The folded-history and reveal routes (#731 S2-2): paged finished work, and where a node sits."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import plans.service as service
from plans import closure, folded_reads
from plans.folded_reads import PAGE_SIZE
from plans.operator_projection import OperatorNotFound
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_plan_operator_api import _actor, _DbProxy, _immediate, _task, db  # noqa: F401 - fixture


def _settle_all(db, node_ids: list[str]) -> list[str]:
    """Accept these nodes on distinct, increasing last-changed times; return ids newest first."""
    with raw_plan_seed(db):
        for index, node_id in enumerate(node_ids):
            db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (node_id,))
            db.execute(
                "UPDATE plan_nodes SET own_last_changed=? WHERE id=?",
                (f"2026-03-01T00:{index // 60:02d}:{index % 60:02d}+00:00", node_id),
            )
    return node_ids[::-1]


def _history(db, count: int = 120):
    plan = service.ensure_plan("thread", "parent", db=db)
    done = [_task(plan.id, f"Done {index}", db=db).id for index in range(count)]
    _task(plan.id, "Still open", db=db)
    return plan, _settle_all(db, done)


def _pages(plan_id: str, db, *, parent: str = "root") -> list[dict]:
    pages, cursor = [], None
    while True:
        page = folded_reads.read_folded(plan_id, parent=parent, cursor=cursor, actor=_actor(), db=db)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages


def test_history_pages_newest_first_without_gaps_or_repeats(db):
    plan, newest_first = _history(db)
    pages = _pages(plan.id, db)
    assert [len(page["items"]) for page in pages] == [PAGE_SIZE, PAGE_SIZE, 20]
    assert [item["id"] for page in pages for item in page["items"]] == newest_first
    assert pages[-1]["next_cursor"] is None
    first = pages[0]["items"][0]
    assert first["state"] == "accepted" and first["display"] == {"label_key": "finished", "tone_key": "complete"}
    assert first["folds"] == {"done": 0, "failed": 0, "closed": 0} and first["has_folded_children"] is False


def test_a_folded_row_carries_why_it_settled(db):
    plan = service.ensure_plan("thread", "parent", db=db)
    closed = _task(plan.id, "Retire the draft", db=db).id
    accepted = _task(plan.id, "Wrote the guide", db=db).id
    _task(plan.id, "Still open", db=db)
    closure.close_node(closed, reason="No longer needed", db=db)
    _settle_all(db, [accepted])
    items = {item["id"]: item for page in _pages(plan.id, db) for item in page["items"]}
    assert items[closed]["status_reason"] == "No longer needed"
    assert items[accepted]["status_reason"] is None
    path = folded_reads.read_path(plan.id, closed, actor=_actor(), db=db)
    assert path["node"]["status_reason"] == "No longer needed"


def test_equal_fold_times_page_by_id_without_losing_a_row(db):
    plan = service.ensure_plan("thread", "parent", db=db)
    ids = [_task(plan.id, f"Same {index}", db=db).id for index in range(PAGE_SIZE + 10)]
    with raw_plan_seed(db):
        for node_id in ids:
            db.execute("UPDATE plan_nodes SET state='accepted',own_last_changed='2026-03-01T00:00:00+00:00' WHERE id=?", (node_id,))
    seen = [item["id"] for page in _pages(plan.id, db) for item in page["items"]]
    assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen))


def test_a_nested_level_pages_beneath_its_own_folded_parent(db):
    plan = service.ensure_plan("thread", "parent", db=db)
    phase = service.add_node(plan.id, kind="phase", title="Wave", acceptance_criteria="Wave DOD", db=db)
    kids = [_task(plan.id, f"Step {index}", db=db, parent_id=phase.id).id for index in range(60)]
    _task(plan.id, "Open root", db=db)
    newest_first = _settle_all(db, kids)
    root = _pages(plan.id, db)
    assert [item["id"] for page in root for item in page["items"]] == [phase.id]
    wave = root[0]["items"][0]
    assert wave["folds"] == {"done": 60, "failed": 0, "closed": 0} and wave["has_folded_children"] is True
    nested = _pages(plan.id, db, parent=phase.id)
    assert [item["id"] for page in nested for item in page["items"]] == newest_first


def test_path_names_ancestors_and_the_page_holding_the_node(db):
    plan, newest_first = _history(db)
    target = newest_first[PAGE_SIZE + 7]
    path = folded_reads.read_path(plan.id, target, actor=_actor(), db=db)
    assert path["ancestors"] == []
    assert path["page"]["index"] == PAGE_SIZE + 7 and path["page"]["page_index"] == 1
    page = folded_reads.read_folded(
        plan.id, parent=path["page"]["parent"], cursor=path["page"]["cursor"], actor=_actor(), db=db,
    )
    assert page["items"][path["page"]["index"] % PAGE_SIZE]["id"] == target
    first = folded_reads.read_path(plan.id, newest_first[3], actor=_actor(), db=db)
    assert first["page"]["cursor"] is None and first["page"]["page_index"] == 0


def test_path_of_a_nested_node_climbs_to_the_root_and_an_open_node_has_no_page(db):
    plan = service.ensure_plan("thread", "parent", db=db)
    phase = service.add_node(plan.id, kind="phase", title="Wave", acceptance_criteria="Wave DOD", db=db)
    done = _task(plan.id, "Done step", db=db, parent_id=phase.id)
    live = _task(plan.id, "Live step", db=db, parent_id=phase.id)
    _settle_all(db, [done.id])
    nested = folded_reads.read_path(plan.id, done.id, actor=_actor(), db=db)
    assert [(item["id"], item["folded"]) for item in nested["ancestors"]] == [(phase.id, False)]
    assert nested["page"]["parent"] == phase.id
    open_node = folded_reads.read_path(plan.id, live.id, actor=_actor(), db=db)
    assert open_node["page"] is None and open_node["node"] is None


def test_refusals_read_alike_for_missing_foreign_and_cross_plan_targets(db):
    plan, newest_first = _history(db, count=3)
    foreign = service.ensure_plan("thread", "foreign", db=db)
    foreign_node = _task(foreign.id, "Foreign work", db=db)
    for call in (
        lambda: folded_reads.read_folded("missing", actor=_actor(), db=db),
        lambda: folded_reads.read_folded(foreign.id, actor=_actor(), db=db),
        lambda: folded_reads.read_folded(plan.id, parent="missing", actor=_actor(), db=db),
        lambda: folded_reads.read_folded(plan.id, parent=foreign_node.id, actor=_actor(), db=db),
        lambda: folded_reads.read_path(plan.id, "missing", actor=_actor(), db=db),
        lambda: folded_reads.read_path(plan.id, foreign_node.id, actor=_actor(), db=db),
        lambda: folded_reads.read_path(foreign.id, foreign_node.id, actor=_actor(), db=db),
    ):
        with pytest.raises(OperatorNotFound):
            call()
    with pytest.raises(ValueError):
        folded_reads.read_folded(plan.id, cursor="no-comma", actor=_actor(), db=db)


@pytest.mark.asyncio
async def test_http_routes_serve_the_owner_and_hide_other_users_plans(db, monkeypatch):
    from api import plans_folded as api_folded
    plan, newest_first = _history(db, count=3)
    proxy = _DbProxy(db)
    monkeypatch.setattr(api_folded, "get_db", lambda: proxy)
    monkeypatch.setattr(api_folded, "run_read", _immediate)
    app = FastAPI()
    app.include_router(api_folded.router)
    app.dependency_overrides[api_folded.require_user] = lambda: {"id": "user-1"}
    with TestClient(app) as client:
        page = client.get(f"/api/plans/{plan.id}/folded")
        assert page.status_code == 200
        assert [item["id"] for item in page.json()["items"]] == newest_first
        assert client.get(f"/api/plans/{plan.id}/folded?cursor=bad").status_code == 422
        path = client.get(f"/api/plans/{plan.id}/nodes/{newest_first[0]}/path")
        assert path.status_code == 200 and path.json()["page"]["index"] == 0
        missing = client.get("/api/plans/missing/folded")
        other = client.get(f"/api/plans/{plan.id}/nodes/missing/path")
        assert (missing.status_code, other.status_code) == (404, 404)
    app.dependency_overrides[api_folded.require_user] = lambda: {"id": "user-2"}
    with TestClient(app) as client:
        refused = client.get(f"/api/plans/{plan.id}/folded")
        refused_path = client.get(f"/api/plans/{plan.id}/nodes/{newest_first[0]}/path")
        assert (refused.status_code, refused_path.status_code) == (404, 404)
        assert refused.json() == missing.json()


def test_folded_reads_use_the_folded_index_not_a_scan(db):
    plan, newest_first = _history(db, count=5)
    where = folded_reads._FOLDED
    for level, extra in ((" AND parent_id IS NULL", ()), (" AND parent_id=?", ("x",))):
        for sql in (
            f"SELECT {folded_reads._COLUMNS} FROM plan_nodes WHERE {where}{level}"
            " AND (folded_at<? OR (folded_at=? AND id>?)) ORDER BY folded_at DESC, id LIMIT ?",
            f"SELECT COUNT(*) FROM plan_nodes WHERE {where}{level} AND (folded_at>? OR (folded_at=? AND id<?))",
        ):
            marks = sql.count("?")
            args = (plan.id, *extra) + ("t", "t", "i", 1)[: marks - 1 - len(extra)]
            detail = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall())
            assert "idx_plan_nodes_folded" in detail and "SCAN" not in detail, detail


def test_the_conductor_tool_policy_leaves_the_read_routes_open():
    from tools.conductor_api_policy import OFF_LIMITS_ROWS
    import re
    for path in ("/api/plans/p1/folded", "/api/plans/p1/nodes/n1/path"):
        assert not [row for row in OFF_LIMITS_ROWS if re.search(row[2], path) and re.search(f"^(?:{row[1]})$", "GET")]


def test_the_chat_list_open_and_finished_probes_use_the_open_and_folded_indexes(db):
    from plans import operator_chat_state

    plan, _ = _history(db, count=3)
    captured: list[str] = []
    db.set_trace_callback(captured.append)
    operator_chat_state._plan_rows(["thread"], db=db)
    db.set_trace_callback(None)
    sql = next(text for text in captured if "has_open" in text)
    detail = " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql).fetchall())
    # Both probes are partial-index searches on the plan; the finished one may take either folded index.
    assert "idx_plan_nodes_open" in detail, detail
    assert "idx_plan_nodes_folded" in detail or "idx_plan_nodes_recent" in detail, detail
    assert "SCAN n" not in detail, detail
