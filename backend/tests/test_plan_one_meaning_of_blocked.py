"""Every read gives the same answer to "why can this not start?" (#2213, #2023).

Two concepts keep open work from starting: an unmet dependency and a hold.
``plans.node_waits`` owns both predicates, so the detail, the agent's list
read, the pane and the status read agree node for node. Red is for failed
work and a hold the user must clear; anyone else's hold is a neutral wait.
"""

from __future__ import annotations

import re
import sqlite3

from infra.schema import SCHEMA_SQL
from plans import holds, operator_projection, service, status, status_context
from plans.boundary import PlanActor, read_current_plan
from tests.plan_blocked_wait_helpers import wait_on

HUMAN = PlanActor(None, "user", None, kind="user")
OWNER = PlanActor("owner", "user", "project")
PROVENANCE = "test.plan.one_meaning_of_blocked"


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','P','t','t')")
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at) "
        "VALUES ('owner','user','project','Plan owner','t','t')"
    )
    db.commit()
    return db


def _task(plan_id: str, title: str, *, db):
    return service.add_node(plan_id, kind="task", title=title,
                            acceptance_criteria=f"{title} is proven", db=db)


def _hold(db, node_id: str, owner_kind: str, reason: str) -> None:
    """Block a task; only the user's own blocker is a hold, any other a wait."""
    if owner_kind != "user":
        wait_on(db, node_id, reason=reason, owner_kind=owner_kind)
        return
    with service._write(db):
        holds.block(
            node_id, reason_key="test_hold", reason=reason, owner_kind=owner_kind,
            owner_id=None, expected_revision=service._node_row(db, node_id)["revision"],
            actor_id="owner", provenance=PROVENANCE, db=db,
        )


def test_every_surface_names_the_same_dependency_waits_and_holds():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        build = _task(plan.id, "Build", db=db)
        ship = _task(plan.id, "Ship", db=db)
        deploy = _task(plan.id, "Deploy", db=db)
        sign = _task(plan.id, "Sign off", db=db)
        service.add_dependency(ship.id, build.id, db=db)
        _hold(db, deploy.id, "external", "Needs production to serve the fix")
        _hold(db, sign.id, "user", "Needs the user's sign-off")
        # The external blocker is a wait, not a hold, so nothing holds it.
        expected = {
            build.id: ([], None), ship.id: ([build.id], None),
            deploy.id: ([], None), sign.id: ([], "user"),
        }

        detail = {node_id: service.get_node(node_id, db=db) for node_id in expected}
        listed = {node["id"]: node for node in read_current_plan(actor=OWNER, db=db)["nodes"]}
        pane = {node["id"]: node for node in
                operator_projection.read_thread("owner", actor=HUMAN, db=db)["plans"][0]["nodes"]}
        for node_id, (unmet, held_by) in expected.items():
            assert (detail[node_id].unmet_dependencies, detail[node_id].held_by) == (unmet, held_by)
            assert (list(listed[node_id]["unmet_dependencies"]), listed[node_id]["held_by"]) == (
                unmet, held_by)
            assert (list(pane[node_id]["unmet_dependencies"]), pane[node_id]["held_by"]) == (
                unmet, held_by)

        # Only moving work reads Working: ready work waits its turn, as does every
        # non-actionable dependency, external wait, or legacy hold.
        assert {node_id: pane[node_id]["display"]["tone_key"] for node_id in expected} == {
            build.id: "waiting", ship.id: "waiting", deploy.id: "waiting", sign.id: "waiting",
        }
        rows = {row["id"]: row for row in
                status.read(db, service._plan_row(db, plan.id), actor=OWNER)["current"]["rows"]}
        assert {node_id: rows[node_id]["condition"] for node_id in (ship.id, deploy.id, sign.id)} == {
            ship.id: "waiting", deploy.id: "waiting", sign.id: "waiting",
        }
    finally:
        db.close()


def test_a_digest_with_nothing_ready_names_the_open_work_instead_of_idle():
    """#2023: 'idle' only when nothing is open; otherwise say what the open work is doing."""
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        assert status_context.projection(
            db, service._plan_row(db, plan.id), actor=OWNER,
        ).get("note") == "idle: nothing open"

        deploy = _task(plan.id, "Deploy", db=db)
        _hold(db, deploy.id, "external", "Needs production to serve the fix")
        thin = status_context.projection(db, service._plan_row(db, plan.id), actor=OWNER)

        # Only waiting is not moving (stuck plans R9): the note names the wait's end.
        ends = re.fullmatch(r"not moving: next wait ends (\S+)\. .+ nothing is owed until (\S+)\.", thin["note"])
        assert ends is not None and ends[1] == ends[2]
        assert thin["open_step"]["id"] == deploy.id
        assert thin["open_step"]["condition"] == "waiting"
        assert thin["open_step"]["detail_read"] == f"plan_read(node_id='{deploy.id}')"
    finally:
        db.close()
