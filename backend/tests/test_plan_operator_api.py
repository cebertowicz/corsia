"""Red-first P5 stages 1-2 contract tests for authenticated operator reads."""
from __future__ import annotations

from collections.abc import Iterator

import sqlite3
from datetime import datetime
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


from infra.schema import SCHEMA_SQL
import plans.artifact_links as artifact_links
import plans.decisions as decisions
import plans.mutations as mutations
import plans.rollup as rollup
import plans.service as service
from plans.boundary import PlanActor
from tests.plan_review_helpers import submit_review_evidence


class _DbProxy:
    """Keep the fixture connection open when an API operation closes its handle."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        return None


async def _immediate(callable_):
    return callable_()


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    for project_id, name in (
        ("project-1", "Project One"),
        ("project-2", "Project Two"),
        ("planless", "No Plans Yet"),
        ("archived-access", "Archived Membership"),
    ):
        connection.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
            (project_id, name, "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
        )
    for values in (
        ("parent", "user-1", "project-1", None, "Parent work", "active", None),
        ("child", "user-1", "project-1", "parent", "Child work", "active", None),
        ("paused", "user-1", "project-1", None, "Paused work", "active", None),
        ("empty-paused", "user-1", "project-1", None, "Empty paused", "active", None),
        ("empty-idle", "user-1", "project-1", None, "Empty idle", "active", None),
        ("archived", "user-1", "project-1", None, "Archived", "archived", "2026-07-02T00:00:00+00:00"),
        ("planless-thread", "user-1", "planless", None, "No plan", "active", None),
        ("archived-project", "user-1", "archived-access", None, "Archived membership", "archived", "2026-07-02T00:00:00+00:00"),
        ("foreign", "user-2", "project-2", None, "Foreign", "active", None),
        ("workspace-thread", "user-1", None, None, "Workspace work", "active", None),
    ):
        connection.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,title,lifecycle_state,archived_at,
                created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (*values, "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
        )
    connection.commit()
    yield connection
    connection.close()


def _actor(user_id: str = "user-1") -> PlanActor:
    return PlanActor(None, user_id, None, kind="user")


def _task(plan_id: str, title: str, *, db, **kwargs):
    return service.add_node(
        plan_id,
        kind="task",
        title=title,
        acceptance_criteria=f"{title} DOD",
        db=db,
        **kwargs,
    )


def _accept(node_id: str, *, db) -> None:
    service.transition_node(node_id, "active", db=db)
    submit_review_evidence(node_id, db=db)
    service.transition_node(
        node_id,
        "accepted",
        verification_result="passed",
        verified_by="reviewer",
        db=db,
    )


def _seed_operator_tree(db) -> dict[str, str]:
    parent = service.ensure_plan("thread", "parent", db=db)
    phase = service.add_node(
        parent.id,
        kind="phase",
        title="Delivery",
        acceptance_criteria="Delivery DOD",
        db=db,
    )
    track = service.add_node(
        parent.id,
        kind="track",
        title="Backend",
        acceptance_criteria="Backend DOD",
        parent_id=phase.id,
        db=db,
    )
    prerequisite = _task(parent.id, "Prerequisite", db=db)
    blocked = _task(parent.id, "Blocked", parent_id=track.id, db=db)
    service.add_dependency(blocked.id, prerequisite.id, db=db)
    next_task = _task(parent.id, "Next", parent_id=track.id, db=db)
    delegated = _task(
        parent.id,
        "Delegated",
        parent_id=track.id,
        spawn_kind="claude-code",
        db=db,
    )
    same_track_later = _task(parent.id, "Later in backend", parent_id=track.id, db=db)
    parallel_track = service.add_node(
        parent.id,
        kind="track",
        title="Docs",
        acceptance_criteria="Docs DOD",
        parent_id=phase.id,
        db=db,
    )
    parallel_next = _task(parent.id, "Document", parent_id=parallel_track.id, db=db)
    service.transition_node(delegated.id, "active", db=db)
    child = service.ensure_plan("thread", "child", db=db)
    child_task = _task(child.id, "Child step", db=db)
    mutations.attach_spawned_thread(delegated.id, "child", db=db)

    paused = service.ensure_plan("thread", "paused", db=db)
    _task(paused.id, "Paused action", db=db)
    db.execute("UPDATE plans SET state='paused' WHERE id=?", (paused.id,))

    empty_paused = service.ensure_plan("thread", "empty-paused", db=db)
    db.execute("UPDATE plans SET state='paused' WHERE id=?", (empty_paused.id,))
    service.ensure_plan("thread", "empty-idle", db=db)
    archived = service.ensure_plan("thread", "archived", db=db)
    _task(archived.id, "Archived task", db=db)
    db.execute(
        "UPDATE plans SET state='archived',archived_at=? WHERE id=?",
        ("2026-07-02T00:00:00+00:00", archived.id),
    )
    workspace_plan = service.ensure_plan("thread", "workspace-thread", db=db)
    _task(workspace_plan.id, "Workspace task", db=db)
    foreign_plan = service.ensure_plan("thread", "foreign", db=db)
    _task(foreign_plan.id, "Foreign task", db=db)
    db.commit()
    return {
        "parent_plan": parent.id,
        "phase": phase.id,
        "track": track.id,
        "prerequisite": prerequisite.id,
        "blocked": blocked.id,
        "next": next_task.id,
        "same_track_later": same_track_later.id,
        "parallel_next": parallel_next.id,
        "delegated": delegated.id,
        "child_plan": child.id,
        "child_task": child_task.id,
        "paused_plan": paused.id,
        "archived_plan": archived.id,
    }


def _projection():
    from plans import operator_projection

    return operator_projection


def _plan_for_owner(payload, owner_id: str):
    return next(item for item in payload["plans"] if item["plan"]["owner_id"] == owner_id)


def _assert_absolute(timestamp: str) -> None:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_authenticated_scope_thread_and_child_reads_share_one_contextual_contract(db):
    ids = _seed_operator_tree(db)
    projection = _projection()
    workspace = projection.read_workspace(actor=_actor(), db=db)
    project = projection.read_project("project-1", actor=_actor(), db=db)
    thread = projection.read_thread("parent", actor=_actor(), db=db)
    child = projection.read_child(ids["delegated"], actor=_actor(), db=db)

    assert {payload["contract_version"] for payload in (workspace, project, thread, child)} == {2}
    assert workspace["target"]["kind"] == "workspace_scope"
    assert "foreign" not in {item["plan"]["owner_id"] for item in workspace["plans"]}
    assert project["target"] == {
        "kind": "project_scope", "id": "project-1", "label": "Project One",
    }
    assert thread["target"] == {
        "kind": "thread", "id": "parent", "label": "Parent work",
    }
    assert child["target"] == {
        "kind": "thread", "id": "child", "label": "Child work",
    }
    assert _plan_for_owner(project, "parent") == thread["plans"][0]
    assert child["plans"][0]["plan"]["id"] == ids["child_plan"]
    assert child["plans"][0]["nodes"][0]["id"] == ids["child_task"]
    delegated = next(
        node for node in thread["plans"][0]["nodes"]
        if node["id"] == ids["delegated"]
    )
    assert delegated["child"]["thread_id"] == "child"


def test_review_requested_names_agent_workflow_instead_of_human_attention(db):
    ids = _seed_operator_tree(db)
    service.transition_node(ids["delegated"], "review_requested", db=db)

    payload = _projection().read_thread("parent", actor=_actor(), db=db)
    delegated = next(
        node for node in payload["plans"][0]["nodes"]
        if node["id"] == ids["delegated"]
    )

    assert delegated["review"] == {
        "kind": "parent_agent",
        "label": "Awaiting the agent's review",
        "actor": {
            "kind": "thread",
            "id": "parent",
            "label": "Parent work",
        },
    }
    assert payload["attention"]["awaiting_count"] == 0


def test_raised_decision_names_the_human_actor_without_relabeling_agent_review(db):
    ids = _seed_operator_tree(db)
    agent = PlanActor("parent", "user-1", "project-1")
    content = {
        "title": "Choose the release shape",
        "context": "The user owns this product decision.",
        "tradeoff": "Each option changes the user experience.",
        "recommendation": "A",
        "rationale": "The right pane preserves context.",
        "options": [
            {"id": "A", "label": "Quiet", "detail": "Use the right pane."},
            {"id": "B", "label": "Loud", "detail": "Add a destination."},
        ],
        "other_enabled": True,
    }
    decision = decisions.draft(
        ids["parent_plan"],
        source_node_id=ids["prerequisite"],
        mutation_id="operator-decision-draft",
        actor=agent,
        provenance="test.operator",
        db=db,
        **content,
    ).result["decision"]
    decision = decisions.parent_review(
        decision["id"],
        expected_revision=decision["revision"],
        mutation_id="operator-decision-review",
        actor=agent,
        provenance="test.operator",
        db=db,
    ).result["decision"]
    decision = decisions.raise_to_user(
        decision["id"],
        expected_revision=decision["revision"],
        mutation_id="operator-decision-raise",
        actor=agent,
        provenance="test.operator",
        db=db,
    ).result["decision"]

    payload = _projection().read_thread("parent", actor=_actor(), db=db)
    review_node = next(
        node for node in payload["plans"][0]["nodes"]
        if node["id"] == decision["node_id"]
    )

    assert review_node["review"] == {
        "kind": "human_decision",
        "label": "Needs your decision",
        "actor": {"kind": "user", "id": None, "label": "You"},
    }
    assert payload["attention"]["awaiting_count"] == 1


def test_thread_read_loads_child_details_only_on_demand(db, monkeypatch):
    ids = _seed_operator_tree(db)
    detailed = Mock(wraps=rollup._rollup_plan)
    monkeypatch.setattr(rollup, "_rollup_plan", detailed)

    thread = _projection().read_thread("parent", actor=_actor(), db=db)

    detailed_ids = {call.args[1]["id"] for call in detailed.call_args_list}
    assert ids["child_plan"] not in detailed_ids
    delegated = next(
        node for node in thread["plans"][0]["nodes"]
        if node["id"] == ids["delegated"]
    )
    assert delegated["child"] == {
        "available": True,
        "plan_id": ids["child_plan"],
        "thread_id": "child",
        "progress": {"done": 0, "total": 1, "open": 1, "liveness": "idle"},
    }
    assert "nodes" not in delegated["child"]


def test_scope_reads_survive_a_child_whose_delegating_step_was_archived(db):
    """#2077: a retired delegating step listed its child twice and read as a cycle."""
    ids = _seed_operator_tree(db)
    db.execute(
        "UPDATE plan_nodes SET archived_at=? WHERE id=?",
        ("2026-07-03T00:00:00+00:00", ids["delegated"]),
    )
    projection = _projection()

    for payload in (
        projection.read_workspace(actor=_actor(), db=db),
        projection.read_project("project-1", actor=_actor(), db=db),
    ):
        child = _plan_for_owner(payload, "child")
        parent = _plan_for_owner(payload, "parent")
        assert [node["id"] for node in child["nodes"]] == [ids["child_task"]]
        assert ids["delegated"] not in {node["id"] for node in parent["nodes"]}


def test_operator_reads_do_not_ensure_reconcile_or_write(db):
    ids = _seed_operator_tree(db)
    projection = _projection()
    before_changes = db.total_changes
    before_counts = {
        table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("plans", "plan_nodes", "plan_node_events")
    }
    projection.read_thread("parent", actor=_actor(), db=db)
    projection.read_child(ids["delegated"], actor=_actor(), db=db)
    assert db.total_changes == before_changes
    assert {
        table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before_counts
    } == before_counts


def test_first_scope_reads_are_empty_and_write_no_plan(db):
    """A scope with no plan yet reads as empty, not as a state awaiting setup."""
    projection = _projection()
    before_changes = db.total_changes
    workspace = projection.read_workspace(actor=_actor(), db=db)
    project = projection.read_project("planless", actor=_actor(), db=db)
    assert workspace["plans"] == [] and project["plans"] == []
    assert project["target"]["label"] == "No Plans Yet"
    assert db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert db.total_changes == before_changes


def test_a_new_project_with_no_threads_reads_empty_not_unavailable(db):
    """Project visibility follows the projects API, not thread membership."""
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
        ("fresh", "Fresh Project", "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
    )
    project = _projection().read_project("fresh", actor=_actor(), db=db)
    assert project["plans"] == []
    assert project["target"]["label"] == "Fresh Project"


def test_a_project_with_plans_stays_unavailable_to_a_non_member(db):
    """Only a plan-less project skips the membership gate; plan content never does."""
    projection = _projection()
    service.ensure_plan("project_scope", "project-2", db=db)
    with pytest.raises(projection.OperatorNotFound, match="operator target is unavailable"):
        projection.read_project("project-2", actor=_actor(), db=db)


def test_legacy_discussion_links_remain_readable_but_cannot_be_created(db):
    ids = _seed_operator_tree(db)
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,state,created_at,updated_at)
           VALUES ('legacy-discussion','local','document','legacy.md','active','t','t')"""
    )
    with pytest.raises(service.PlanError, match="invalid plan artifact role"):
        artifact_links.link(
            ids["delegated"],
            "legacy-discussion",
            "discussion",
            actor_id="parent",
            provenance="test.operator",
            db=db,
        )
    db.execute(
        """INSERT INTO plan_node_artifact_links
           (node_id,artifact_id,role,actor_id,provenance,created_at)
           VALUES (?,?,'discussion','parent','legacy import','t')""",
        (ids["delegated"], "legacy-discussion"),
    )
    db.commit()

    before_changes = db.total_changes
    payload = _projection().read_thread("parent", actor=_actor(), db=db)
    delegated = next(
        node for node in payload["plans"][0]["nodes"]
        if node["id"] == ids["delegated"]
    )

    assert delegated["discussion_artifact_id"] == "legacy-discussion"
    assert db.total_changes == before_changes


def test_foreign_missing_and_purged_targets_fail_without_disclosure(db):
    ids = _seed_operator_tree(db)
    projection = _projection()
    db.execute("UPDATE threads SET purged_at=? WHERE id='paused'", ("2026-07-03T00:00:00+00:00",))

    for thread_id in ("foreign", "missing", "paused"):
        with pytest.raises(projection.OperatorNotFound, match="operator target is unavailable"):
            projection.read_thread(thread_id, actor=_actor(), db=db)
    with pytest.raises(projection.OperatorNotFound, match="operator target is unavailable"):
        projection.read_child("missing-node", actor=_actor(), db=db)
    with pytest.raises(projection.OperatorNotFound, match="operator target is unavailable"):
        projection.read_child(ids["delegated"], actor=_actor("user-2"), db=db)


def test_an_archived_worker_keeps_a_readable_plan(db):
    ids = _seed_operator_tree(db)
    db.execute(
        "UPDATE threads SET archived_at=?, lifecycle_state='archived' WHERE id='child'",
        ("2026-07-03T00:00:00+00:00",),
    )
    projection = _projection()

    for payload in (
        projection.read_child(ids["delegated"], actor=_actor(), db=db),
        projection.read_thread("child", actor=_actor(), db=db),
    ):
        assert payload["target"]["label"] == "Child work"
        assert [node["id"] for node in payload["plans"][0]["nodes"]] == [ids["child_task"]]
    # Rollups still list only live threads.
    project = projection.read_project("project-1", actor=_actor(), db=db)
    assert "child" not in {plan["owner"]["thread_id"] for plan in project["plans"]}


def test_an_archived_worker_with_a_step_up_for_review_stays_readable(db):
    """The common case: the worker puts its step up for its parent and is archived."""
    ids = _seed_operator_tree(db)
    service.transition_node(ids["child_task"], "active", db=db)
    submit_review_evidence(ids["child_task"], db=db)
    db.execute(
        "UPDATE threads SET archived_at=?, lifecycle_state='archived' WHERE id='child'",
        ("2026-07-03T00:00:00+00:00",),
    )
    projection = _projection()

    for payload in (
        projection.read_child(ids["delegated"], actor=_actor(), db=db),
        projection.read_thread("child", actor=_actor(), db=db),
    ):
        step = payload["plans"][0]["nodes"][0]
        assert step["review"]["actor"] == {"kind": "thread", "id": "parent", "label": "Parent work"}
    projection.read_project("project-1", actor=_actor(), db=db)


def test_a_thread_without_a_plan_reads_as_an_empty_plan(db):
    _seed_operator_tree(db)
    payload = _projection().read_thread("planless-thread", actor=_actor(), db=db)
    assert payload["target"] == {"kind": "thread", "id": "planless-thread", "label": "No plan"}
    assert payload["plans"] == []


def test_blocked_paths_liveness_and_server_owned_current_next(db):
    ids = _seed_operator_tree(db)
    parent = _projection().read_thread("parent", actor=_actor(), db=db)["plans"][0]
    nodes = {node["id"]: node for node in parent["nodes"]}

    # Its only blocked leaf is sequenced behind open work: a wait, not a block.
    assert parent["progress"]["liveness"] == "waiting"
    assert nodes[ids["blocked"]]["attention_path"] == [
        ids["phase"], ids["track"], ids["blocked"],
    ]
    assert nodes[ids["next"]]["attention_path"] == []
    assert ids["blocked"] in parent["current_node_ids"]
    assert parent["next_node_ids"] == [ids["next"]]
    assert ids["parallel_next"] not in parent["next_node_ids"]
    assert ids["prerequisite"] not in parent["next_node_ids"]
    assert ids["same_track_later"] not in parent["next_node_ids"]



def test_operator_fields_have_absolute_freshness_revisions_and_unwitnessed_completion(db):
    ids = _seed_operator_tree(db)
    _accept(ids["next"], db=db)
    payload = _projection().read_thread("parent", actor=_actor(), db=db)
    plan = payload["plans"][0]
    completed = next(node for node in plan["nodes"] if node["id"] == ids["next"])
    _assert_absolute(payload["as_of"])
    _assert_absolute(plan["plan"]["updated_at"])
    _assert_absolute(completed["updated_at"])
    _assert_absolute(completed["completion"]["at"])
    assert isinstance(plan["plan"]["revision"], int)
    assert isinstance(completed["revision"], int)
    assert completed["completion"]["event_id"]
    assert completed["completion"]["witnessed"] is False
    assert len(payload["revision_token"]) == 64


@pytest.mark.asyncio
async def test_operator_http_routes_emit_etag_revalidate_and_hide_target_existence(db, monkeypatch):
    from api import plans as api_plans
    ids = _seed_operator_tree(db)
    proxy = _DbProxy(db)
    monkeypatch.setattr(api_plans, "get_db", lambda: proxy)
    monkeypatch.setattr(api_plans, "run_in_db", _immediate)
    app = FastAPI()
    app.include_router(api_plans.router)
    app.dependency_overrides[api_plans.require_user] = lambda: {"id": "user-1"}
    with TestClient(app) as client:
        # Canonical Activity path: the same authenticated user owns a live session
        # in current Project One, so this must be a readable rollup, not a 404.
        project = client.get("/api/plans/operator/scopes/projects/project-1")
        assert project.status_code == 200
        assert project.json()["target"] == {
            "kind": "project_scope", "id": "project-1", "label": "Project One",
        }
        first = client.get("/api/plans/operator/threads/parent")
        assert first.status_code == 200
        assert first.headers["etag"] == f'"{first.json()["revision_token"]}"'
        unchanged = client.get(
            "/api/plans/operator/threads/parent",
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert unchanged.status_code == 304
        assert unchanged.content == b""
        current = service.get_node(ids["next"], db=db)
        service.transition_node(ids["next"], "active", expected_revision=current.revision, db=db)
        db.commit()
        changed = client.get(
            "/api/plans/operator/threads/parent",
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert changed.status_code == 200
        assert changed.headers["etag"] != first.headers["etag"]
        missing = client.get("/api/plans/operator/threads/missing")
        foreign = client.get("/api/plans/operator/threads/foreign")
        assert {missing.status_code, foreign.status_code} == {404}
        assert missing.json() == foreign.json()
        archived = client.get("/api/plans/operator/threads/archived")
        assert archived.status_code == 200
        assert archived.json()["plans"] == []
        planless = client.get("/api/plans/operator/scopes/projects/planless")
        assert planless.status_code == 200
        assert planless.json()["plans"] == []
        # #1555: Activity's default scope reads with no workspace plan, never a 404.
        workspace = client.get("/api/plans/operator/scopes/workspace")
        assert workspace.status_code == 200
        assert workspace.json()["target"]["kind"] == "workspace_scope"
        # The chat list reads the caller's own threads only; others stay absent.
        states = client.get("/api/plans/operator/thread-states?ids=parent,foreign,,missing")
        assert states.status_code == 200
        assert set(states.json()["states"]) == {"parent"}
        assert client.get("/api/plans/operator/thread-states").json() == {"states": {}}
        over = ",".join(f"t{index}" for index in range(api_plans.public.THREAD_STATE_LIMIT + 1))
        assert client.get(f"/api/plans/operator/thread-states?ids={over}").status_code == 422
    routes = {route.path for route in api_plans.router.routes}
    assert routes >= {
        "/api/plans/operator/thread-states",
        "/api/plans/operator/scopes/workspace",
        "/api/plans/operator/scopes/projects/{project_id}",
        "/api/plans/operator/threads/{thread_id}",
        "/api/plans/operator/nodes/{node_id}/child",
    }


def test_a_step_awaiting_its_owners_verdict_names_the_owner_as_reviewer(db):
    """A harvest puts a delegated step up for its own plan's owner, not for the owner's parent.

    Once the owner puts it up itself, it is still the owner's while other work
    can move, and becomes the parent's when it is the return (matrix G4).
    """
    ids = _seed_operator_tree(db)
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,spawned_by,title,created_at,updated_at)
           VALUES ('leaf','user-1','project-1','child','Leaf work','t','t')"""
    )
    child_plan = service.ensure_plan("thread", "child", db=db)
    delegated = _task(child_plan.id, "Judge the leaf", spawn_kind="claude-code", db=db)
    service.transition_node(delegated.id, "active", db=db)
    service.ensure_plan("thread", "leaf", db=db)
    mutations.attach_spawned_thread(delegated.id, "leaf", db=db)
    service.transition_node(
        delegated.id, "review_requested", provenance="plan.delegation_harvest", db=db,
    )

    def review():
        payload = _projection().read_thread("child", actor=_actor(), db=db)
        return next(
            node["review"] for node in payload["plans"][0]["nodes"]
            if node["id"] == delegated.id
        )

    assert review()["actor"] == {"kind": "thread", "id": "child", "label": "Child work"}

    submit_review_evidence(delegated.id, db=db, actor_id="child")

    assert review()["actor"] == {"kind": "thread", "id": "child", "label": "Child work"}

    from plans import closure

    closure.close_node(ids["child_task"], reason="Not needed", db=db)

    assert review()["actor"] == {"kind": "thread", "id": "parent", "label": "Parent work"}


@pytest.mark.asyncio
async def test_a_pane_view_nudges_that_threads_chat_row(db, monkeypatch):
    """Seeing the plan clears its failed-step dot, so the user's devices re-read that row."""
    from api import plans as api_plans
    _seed_operator_tree(db)
    proxy = _DbProxy(db)
    nudges: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(api_plans, "get_db", lambda: proxy)
    monkeypatch.setattr(api_plans, "run_in_db", _immediate)
    monkeypatch.setattr(
        api_plans.thread_updates, "notify_changed",
        lambda user_id, thread_ids: nudges.append((user_id, list(thread_ids))),
    )
    app = FastAPI()
    app.include_router(api_plans.router)
    app.dependency_overrides[api_plans.require_user] = lambda: {"id": "user-1"}
    with TestClient(app) as client:
        as_of = client.get("/api/plans/operator/threads/parent").json()["as_of"]
        refused = client.post("/api/plans/operator/threads/parent/seen", json={"seen_through": "yesterday"})
        assert refused.status_code == 422
        assert nudges == []
        assert client.post("/api/plans/operator/threads/parent/seen", json={"seen_through": as_of}).status_code == 200
    assert nudges == [("user-1", ["parent"])]


@pytest.mark.asyncio
async def test_a_pane_view_advances_the_thread_read_and_its_etag(db, monkeypatch):
    """The pane reports what it showed; the next thread read carries it, never a 304."""
    from api import plans as api_plans
    _seed_operator_tree(db)
    proxy = _DbProxy(db)
    monkeypatch.setattr(api_plans, "get_db", lambda: proxy)
    monkeypatch.setattr(api_plans, "run_in_db", _immediate)
    app = FastAPI()
    app.include_router(api_plans.router)
    app.dependency_overrides[api_plans.require_user] = lambda: {"id": "user-1"}
    with TestClient(app) as client:
        first = client.get("/api/plans/operator/threads/parent")
        assert first.json()["seen_through"] is None
        assert "folded_through" not in first.json()
        seen = client.post(
            "/api/plans/operator/threads/parent/seen",
            json={"seen_through": first.json()["as_of"]},
        )
        assert seen.status_code == 200
        assert seen.json()["seen_through"] <= first.json()["as_of"].replace("Z", "+00:00")
        after = client.get(
            "/api/plans/operator/threads/parent",
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert after.status_code == 200
        assert after.json()["seen_through"] == seen.json()["seen_through"]
        bad = client.post(
            "/api/plans/operator/threads/parent/seen",
            json={"seen_through": "yesterday"},
        )
        assert bad.status_code == 422
        for target in ("foreign", "missing"):
            refused = client.post(
                f"/api/plans/operator/threads/{target}/seen",
                json={"seen_through": first.json()["as_of"]},
            )
            assert refused.status_code == 404
    assert "/api/plans/operator/threads/{thread_id}/seen" in {
        route.path for route in api_plans.router.routes
    }


def test_operator_node_carries_its_assignee(db):
    """The pane excludes user-assigned tasks from the Scheduled group, so it needs the field."""
    plan = service.ensure_plan("thread", "parent", db=db)
    mine = _task(plan.id, "Mine", db=db, assignee="user")
    agent = _task(plan.id, "Agent's", db=db)

    payload = _projection().read_thread("parent", actor=_actor(), db=db)
    nodes = {node["id"]: node for node in payload["plans"][0]["nodes"]}

    assert nodes[mine.id]["assignee"] == "user"
    assert nodes[agent.id]["assignee"] is None
