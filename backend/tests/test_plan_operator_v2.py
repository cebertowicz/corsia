"""Contract-v2 proof for the one truthful Plans operator projection."""

from __future__ import annotations

import sqlite3

from infra.schema import SCHEMA_SQL
from plans import admission, review_evidence, service, watch_operations
from plans.boundary import PlanActor
from tests.plan_scene import _artifact
from tests.plan_writer_authorizer import raw_plan_seed


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project','Project','t','t')"
    )
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,title,created_at,updated_at)
           VALUES ('owner','user','project','Plan owner','t','t')"""
    )
    db.commit()
    return db


def _task(plan_id: str, title: str, *, db):
    return service.add_node(
        plan_id,
        kind="task",
        title=title,
        acceptance_criteria=f"{title} is proven",
        db=db,
    )


def test_operator_contract_v2_projects_canonical_execution_evidence_and_cost():
    from plans import operator_projection

    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        running = _task(plan.id, "Running", db=db)
        waiting = _task(plan.id, "Waiting", db=db)
        ready = _task(plan.id, "Ready", db=db)
        review = _task(plan.id, "Review", db=db)
        pull_request = _artifact(db, external_id="owner/repo#1", owner="owner")

        with service._write(db):
            # The owner holds one slot in its plan: the watch frees it for Running.
            admission.activate(
                waiting.id,
                expected_revision=waiting.revision,
                actor_id="owner",
                provenance="test.operator.v2",
                db=db,
            )
            watch_operations.arm(
                plan.id,
                {
                    "node_id": waiting.id,
                    "condition": {"type": "event", "value": "ci.complete"},
                    "artifact_id": pull_request,
                    "expected_event": "ci.complete",
                },
                PlanActor("owner", "user", "project"),
                db,
            )
            admission.activate(
                running.id,
                expected_revision=running.revision,
                actor_id="owner",
                provenance="test.operator.v2",
                db=db,
            )
            service._transition(
                db,
                review.id,
                "active",
                actor_id="owner",
                provenance="test.operator.v2",
                verification_result=None,
                verified_by=None,
            )
            review_row = service._node_row(db, review.id)
            review_evidence.submit(
                review.id,
                evidence="Focused checks passed.",
                expected_revision=review_row["revision"],
                actor_id="owner",
                provenance="test.operator.v2",
                db=db,
            )

        payload = operator_projection.read_thread(
            "owner",
            actor=PlanActor(None, "user", None, kind="user"),
            db=db,
        )
        projected = payload["plans"][0]
        nodes = {node["id"]: node for node in projected["nodes"]}

        assert payload["contract_version"] == 2
        assert nodes[running.id]["execution"]["condition"] == "running"
        assert nodes[running.id]["display"] == {
            "label_key": "working",
            "tone_key": "active",
        }
        assert nodes[running.id]["executor"] == {
            "thread_id": "owner",
            "label": "Plan owner",
        }
        assert nodes[running.id]["requirements_revision"] == 1
        assert nodes[running.id]["commitment_id"] == running.id
        assert len(nodes[running.id]["execution"]["attempts"]) == 1
        assert nodes[waiting.id]["execution"]["condition"] == "waiting"
        assert nodes[waiting.id]["display"]["label_key"] == "waiting"
        assert nodes[waiting.id]["execution"]["selection"] is None
        assert nodes[review.id]["execution"]["condition"] == "review"
        assert nodes[review.id]["evidence"] == {
            "evidence": "Focused checks passed.",
            "artifact_ids": [],
            "requirements_revision": 1,
        }
        assert nodes[review.id]["requires_user_action"] is False
        assert nodes[ready.id]["execution"]["condition"] == "ready"
        assert nodes[ready.id]["display"]["label_key"] == "waiting"
        assert nodes[ready.id]["status_reason"] == "Not started"
        assert nodes[ready.id]["commitment_cost"]["commitment_id"] == ready.id
        assert set(projected["current_node_ids"]) == {
            running.id,
            waiting.id,
            review.id,
        }
        assert ready.id not in projected["next_node_ids"]
        assert projected["progress"]["distribution"] == {
            "done": 0,
            "active": 1,
            "pending_ready": 1,
            "review_parent_agent": 1,
            "review_human": 0,
            "waiting": 1,
            "blocked": 0,
            "paused": 0,
            "failed": 0,
        }
        wait = projected["display"].pop("wait")
        assert projected["display"] == {
            "label_key": "working",
            "tone_key": "active",
        }
        # EW3: an untitled watched artifact is named as the item it opens; its id lets the surface open it.
        assert wait["label"] == "the linked item"
        assert wait["artifact_id"] and wait["more"] == 0
    finally:
        db.close()


def test_missing_executor_is_a_visible_blocker_not_a_projection_error():
    from plans import operator_projection

    db = _db()
    try:
        scope = service.ensure_plan("project_scope", "project", db=db)
        _task(scope.id, "Unassigned", db=db)

        payload = operator_projection.read_project(
            "project",
            actor=PlanActor(None, "user", None, kind="user"),
            db=db,
        )
        projected = next(
            item for item in payload["plans"] if item["plan"]["id"] == scope.id
        )["nodes"][0]

        assert projected["executor"] == {"thread_id": None, "label": "Unassigned"}
        assert projected["execution"]["condition"] == "blocked"
        assert projected["execution"]["causes"][0] == {
            "kind": "blocked",
            "reason_key": "assignment_missing",
            "reason": "No executor is assigned",
            "owner_kind": "system",
            "owner_id": None,
        }
    finally:
        db.close()


def test_operator_projects_branch_progress_from_descendant_leaves():
    from plans import operator_projection

    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        phase = service.add_node(
            plan.id,
            kind="phase",
            title="Phase",
            acceptance_criteria="Children are complete",
            db=db,
        )
        done = service.add_node(
            plan.id,
            kind="task",
            title="Done",
            acceptance_criteria="Done is proven",
            parent_id=phase.id,
            db=db,
        )
        service.add_node(
            plan.id,
            kind="task",
            title="Open",
            acceptance_criteria="Open is proven",
            parent_id=phase.id,
            db=db,
        )
        with raw_plan_seed(db):
            db.execute(
                "UPDATE plan_nodes SET state='accepted' WHERE id=?",
                (done.id,),
            )
        db.commit()

        payload = operator_projection.read_thread(
            "owner",
            actor=PlanActor(None, "user", None, kind="user"),
            db=db,
        )
        nodes = {node["id"]: node for node in payload["plans"][0]["nodes"]}

        assert nodes[phase.id]["branch_progress"] == {"done": 1, "total": 2}
        assert nodes[done.id]["branch_progress"] is None
    finally:
        db.close()


def test_a_container_is_dated_by_its_last_child_not_its_own_edits():
    """A finished wave read "yesterday" when its last child closed today (2026-09-29)."""
    from plans import operator_projection

    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        phase = service.add_node(
            plan.id, kind="phase", title="Wave",
            acceptance_criteria="Children are complete", db=db,
        )
        first = service.add_node(
            plan.id, kind="task", title="First",
            acceptance_criteria="First is proven", parent_id=phase.id, db=db,
        )
        last = service.add_node(
            plan.id, kind="task", title="Last",
            acceptance_criteria="Last is proven", parent_id=phase.id, db=db,
        )
        # Events are append-only; these later ones are each node's latest. The seed
        # rebuilds the stored aggregates (own_last_changed, folded_at) from them on exit.
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (first.id,))
            db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (last.id,))
            for event_id, node_id, at in (
                ("first-done", first.id, "2099-09-28T10:00:00+00:00"),
                ("wave-retitled", phase.id, "2099-09-28T18:07:00+00:00"),
                ("last-closed", last.id, "2099-09-29T13:06:00+00:00"),
            ):
                db.execute(
                    "INSERT INTO plan_node_events (id,node_id,provenance,event,created_at)"
                    " VALUES (?,?,'test','state_transition',?)",
                    (event_id, node_id, at),
                )
                db.execute(
                    "UPDATE plan_nodes SET own_last_changed=? WHERE id=?", (at, node_id),
                )
        db.commit()

        payload = operator_projection.read_thread(
            "owner", actor=PlanActor(None, "user", None, kind="user"), db=db,
        )
        nodes = {node["id"]: node for node in payload["plans"][0]["nodes"]}

        assert nodes[phase.id]["last_transition_at"] == "2099-09-29T13:06:00+00:00"
        # The wave is finished, so its children sit behind its fold: dated through the
        # wave, not listed.
        assert first.id not in nodes and last.id not in nodes
    finally:
        db.close()


def test_operator_projection_1000_nodes_has_bounded_query_count():
    """Large plans remain one batched read, not one query bundle per node."""

    from plans import operator_projection

    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        with service._write(db):
            for index in range(1000):
                _task(plan.id, f"Task {index:04d}", db=db)

        statements: list[str] = []
        # A packed read (infra.packed_rows) first prepares its column names with
        # LIMIT 0, which reads nothing; the read itself is counted.
        db.set_trace_callback(
            lambda statement: (
                statements.append(statement)
                if statement.lstrip().upper().startswith(("SELECT", "WITH"))
                and not statement.rstrip().endswith(") LIMIT 0")
                else None
            )
        )
        payload = operator_projection.read_thread(
            "owner",
            actor=PlanActor(None, "user", None, kind="user"),
            db=db,
        )
        db.set_trace_callback(None)

        assert len(payload["plans"][0]["nodes"]) == 1000
        # One batched read per kind of fact, including each kind of wait, so the
        # bound grows with the vocabulary and never with the number of nodes.
        # node_cost adds a fixed six (the plan tree, its node list, usage,
        # recording failures, execution, and attached-child lookup) at any
        # delegation depth, except that its three id-list queries repeat per
        # chunk of ids, since one IN (...) may not exceed SQLite's
        # host-parameter limit. The rollup's node rows read holds as well as
        # dependencies (`node_waits.project`), since a row names who holds it.
        # Waiting time (`node_waiting`) adds six more id-list reads per chunk
        # (node rows, their events, live edges, watches, user holds, PR links)
        # and one per chunk of prerequisites, of which this plan has none, and
        # one per chunk of plan ids for whole-plan pauses (one plan: one read).
        # The pane reads open work (#731 S2-2), so each fact is read for the listed
        # nodes alone, by id, where it once read the whole plan: nineteen reads
        # repeat per chunk of ids (measured 60, 98 and 136 statements at 300, 1000
        # and 2000 nodes: 41 fixed, 19 per chunk).
        from plans import node_cost

        chunks = -(-1000 // node_cost._ID_CHUNK)
        assert len(statements) <= 41 + 19 * chunks + 1
    finally:
        db.set_trace_callback(None)
        db.close()


def test_attempt_history_is_bounded_while_the_true_count_stays_visible():
    from plans import operator_node_state, operator_projection

    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        node = _task(plan.id, "Retried", db=db)
        with service._write(db):
            admission.activate(
                node.id, expected_revision=node.revision, actor_id="owner",
                provenance="test.operator.v2", db=db,
            )
            churn = operator_node_state.ATTEMPT_HISTORY_LIMIT + 5
            for _ in range(churn - 1):
                assert admission.release(node.id, "retry", db=db)
                attempt, conflict = admission.select(
                    node.id, actor_id="owner", provenance="test.operator.v2", db=db,
                )
                assert conflict is None and attempt is not None

        payload = operator_projection.read_thread(
            "owner", actor=PlanActor(None, "user", None, kind="user"), db=db,
        )
        execution = payload["plans"][0]["nodes"][0]["execution"]

        assert execution["attempt_count"] == churn
        assert len(execution["attempts"]) == operator_node_state.ATTEMPT_HISTORY_LIMIT
        ordinals = [attempt["ordinal"] for attempt in execution["attempts"]]
        assert ordinals == sorted(ordinals)
        assert ordinals[-1] == churn
    finally:
        db.close()


def test_a_failed_node_stays_visible_and_its_follow_up_is_the_owned_next_action():
    """Failure is not disappearance. The failed row keeps its own state and the
    linked follow-up names the same executor, the plan owner, so the plan always
    shows who owns the recovery even when nothing recorded why the work failed."""
    from plans import followups, operator_projection

    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        node = _task(plan.id, "Doomed", db=db)
        with service._write(db):
            admission.activate(
                node.id, expected_revision=node.revision, actor_id="owner",
                provenance="test.operator.v2", db=db,
            )
        service.transition_node(node.id, "failed", db=db)
        admission.release(node.id, "failed", db=db)
        recovery = followups.add_follow_up(
            node.id, title="Recover the failed work",
            acceptance_criteria="The recovery is verified", actor_id="owner",
            provenance="test.operator.v2", db=db,
        )
        with service._write(db):
            admission.activate(
                recovery.id, expected_revision=recovery.revision, actor_id="owner",
                provenance="test.operator.v2", db=db,
            )

        payload = operator_projection.read_thread(
            "owner", actor=PlanActor(None, "user", None, kind="user"), db=db,
        )
        projected = payload["plans"][0]
        nodes = {entry["id"]: entry for entry in projected["nodes"]}

        # No cause was ever recorded, so the row must carry its own failed state.
        assert nodes[node.id]["state"] == "failed"
        assert nodes[node.id]["display"] == {
            "label_key": "finished", "tone_key": "failed",
        }
        assert nodes[node.id]["execution"]["causes"] == []
        assert nodes[node.id]["execution"]["primary_reason"] is None
        assert projected["progress"]["distribution"]["failed"] == 1

        assert recovery.follow_up_of == node.id
        assert nodes[recovery.id]["executor"] == nodes[node.id]["executor"] == {
            "thread_id": "owner", "label": "Plan owner",
        }
        assert nodes[recovery.id]["execution"]["condition"] == "running"
        assert nodes[recovery.id]["display"]["label_key"] == "working"
    finally:
        db.close()
