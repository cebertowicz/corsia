"""A settled assignment retires its temporary worker through the lifecycle owner.

Once the parent's verdict settles the delegated step -- accepted, or failed as
a cancellation -- the worker has nothing left to do for it. A rejection settles
nothing: the same worker holds the revision it creates. Its task-specific
watches and schedules are retired, the artifacts it owned pass to the parent,
and the worker is archived by ``core.thread_lifecycle``, the one owner of a
thread's lifecycle. Archiving is not evidence deletion: the worker's plan, its
events and its review evidence stay readable. A durable roster owner is never
retired this way, and an explicit new assignment brings a retired worker back.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from automation import schedules, watches
from core import thread_lifecycle_trims
from core.thread_lifecycle import run_retention_sweep
from automation.continuation import runner
from automation.continuation.sources.harvest import HarvestSource
from infra.schema import SCHEMA_SQL
from plans import mutations, review_evidence, service
from plans.boundary import PlanActor, mutate
from registry import models
from registry.registration_events import routing_registrations
from tests.plan_review_helpers import submit_review_evidence

NOW = "2026-08-01T00:00:00+00:00"
PARENT = PlanActor("parent", "user-1", "project-1")
PR = {"integration": "github", "type": "pull_request", "external_id": "owner/repo#7"}


def _db(monkeypatch, *, roster_worker: bool = False) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    for thread_id, spawned_by in (("parent", None), ("worker", "parent")):
        db.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,agent_enabled,created_at,updated_at)
               VALUES (?,'user-1','project-1',?,'test/model',?,'t','t')""",
            (thread_id, spawned_by, 1 if roster_worker and thread_id == "worker" else 0),
        )
    db.commit()
    return db


def _returned(db, *titles: str):
    """A harvested delegation with the worker's task-specific state around it."""
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    delegated = service.add_node(
        parent_plan.id, kind="task", title="Delegate work",
        acceptance_criteria="Worker result is reviewed",
        spawn_kind="claude-code", db=db,
    )
    service.transition_node(delegated.id, "active", db=db)
    plan = service.ensure_plan("thread", "worker", db=db)
    mutations.attach_spawned_thread(delegated.id, "worker", db=db)
    leaves = []
    for title in titles:
        leaf = service.add_node(
            plan.id, kind="task", title=title, acceptance_criteria="Done", db=db,
        )
        service.transition_node(leaf.id, "active", actor_id="worker", db=db)
        submit_review_evidence(leaf.id, db=db, evidence=f"{title} done", actor_id="worker")
        leaves.append(leaf)
    models.register_artifact(**PR, parent_thread_id="worker", db=db)
    db.execute("BEGIN")
    watch = watches.arm_watch(
        "owner/repo#7", {"type": "deadline", "value": "2030-01-01T00:00:00+00:00"},
        owner_thread="worker", db=db,
    )
    schedule = schedules.create(
        "worker", "project-1", "worker", kind="cadence", spec="60", max_runs=5,
        now="2026-01-01T00:00:00+00:00", db=db,
    )
    db.commit()
    asyncio.run(runner.sweep(
        NOW, db=db, sources=[HarvestSource()], deliver=AsyncMock(return_value=True),
    ))
    return parent_plan, delegated, plan, leaves, watch, schedule


def _verdict(db, plan_id: str, operation: str, node_id: str) -> dict:
    return mutate(
        plan_id, operation,
        {
            "node_id": node_id, "verification_result": "passed",
            "expected_revision": service._node_row(db, node_id)["revision"],
        },
        actor=PARENT, provenance="test.retirement", mutation_id=uuid.uuid4().hex, db=db,
    ).result


def _lifecycle(db, thread_id: str) -> tuple:
    row = db.execute(
        "SELECT lifecycle_state,archived_reason FROM threads WHERE id=?", (thread_id,)
    ).fetchone()
    return (row["lifecycle_state"] or "active", row["archived_reason"])


@pytest.mark.parametrize("integration,kind", [("claude-code", "session"), ("codex", "thread")])
def test_settlement_keeps_provider_identity_with_its_launcher(monkeypatch, integration, kind):
    from registry.cc_session import registered_confinement
    from subagents.cc_invocation import UNCONFINED

    db = _db(monkeypatch)
    try:
        parent_plan, delegated, *_ = _returned(db, "Work")
        for thread in ("parent", "worker"):
            metadata = None
            if integration == "claude-code" and thread == "worker":
                metadata = {"claude_invocation": {"confinement": "review", "add_dirs": ["/src"],
                                                   "working_dir": "/worker/confined"}}
            models.register_artifact(integration=integration, type=kind, external_id=thread,
                                     parent_thread_id=thread, metadata=metadata, db=db)
        db.execute("UPDATE artifact_identities SET updated_at='9999' WHERE external_id='worker'")
        db.commit()
        submit_review_evidence(delegated.id, db=db, actor_id="parent")
        _verdict(db, parent_plan.id, "accept", delegated.id)
        assert _lifecycle(db, "worker") == ("archived", "assignment_settled")
        assert models.find_for_thread("parent", integration, kind, db=db)["external_id"] == "parent"
        assert registered_confinement("parent", db) == UNCONFINED
        if integration == "claude-code":
            assert registered_confinement("worker", db).mode == "review"
    finally:
        db.close()


@pytest.mark.parametrize("parent_ended", [False, True])
def test_settled_worker_document_passes_to_live_ancestor(monkeypatch, tmp_path, parent_ended):
    db = _db(monkeypatch)
    try:
        db.execute("INSERT INTO threads (id,user_id,project_id,created_at,updated_at) "
                   "VALUES ('grandparent','user-1','project-1','t','t')")
        db.execute("UPDATE threads SET spawned_by='grandparent' WHERE id='parent'")
        db.commit()
        parent_plan, delegated, _plan, _leaves, _watch, _schedule = _returned(db, "Work")
        document = {"integration": "workspace", "type": "document",
                    "external_id": "workspace://ws-1/shared/result.md"}
        path = tmp_path / "result.md"
        path.write_text("Worker result")
        models.register_artifact(**document, path=str(path), parent_thread_id="worker", db=db)
        if parent_ended:
            db.execute("UPDATE threads SET lifecycle_state='archived' WHERE id='parent'")
        submit_review_evidence(delegated.id, db=db, actor_id="parent")
        _verdict(db, parent_plan.id, "accept", delegated.id)
        state = routing_registrations(**document, db=db)
        assert state["owner"] is not None
        assert state["owner"]["parent_thread_id"] == ("grandparent" if parent_ended else "parent")
        assert state["identity"]["state"] == "active"
        assert _lifecycle(db, "worker") == ("archived", "assignment_settled")
    finally:
        db.close()


def test_the_verdict_retires_the_worker_and_keeps_its_evidence(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, _plan, (leaf,), watch, schedule = _returned(
            db, "Write the tests",
        )
        submit_review_evidence(delegated.id, db=db, actor_id="parent")

        _verdict(db, parent_plan.id, "accept", delegated.id)
        db.commit()

        assert _lifecycle(db, "worker") == ("archived", "assignment_settled")
        assert watches.get_watch(watch.id, db=db).status != "armed"
        assert schedules.get(schedule.id, db=db).state == "cancelled"
        # Archiving transfers continuing obligations to the parent.
        state = routing_registrations(**PR, db=db)
        assert state["owner"]["parent_thread_id"] == "parent"
        assert state["triage"] is None
        # Archiving is not evidence deletion.
        node = service.get_node(leaf.id, db=db)
        assert node.state == "accepted"
        assert review_evidence.latest(
            leaf.id, requirements_revision=node.requirements_revision, db=db,
        )["evidence"] == "Write the tests done"
    finally:
        db.close()


@pytest.mark.parametrize("retention", ["ephemeral", "durable"])
def test_settled_worker_trim_keeps_thread_folder_identity(
    monkeypatch, tmp_path, retention,
):
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    db = _db(monkeypatch)
    try:
        project = tmp_path / "projects" / "project"
        project.mkdir(parents=True)
        db.execute("UPDATE projects SET path=?", (str(project),))
        unit = project / ".threads" / "worker" / "working" / "unit"
        nested = unit / "a" / "b" / "result.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("keep")
        stale = project / ".threads" / "worker" / "working" / "stale"
        stale.write_text("remove")
        fresh = project / ".threads" / "worker" / "working" / "fresh"
        fresh.write_text("keep")
        for path, days in ((nested, 8), (stale, 8), (fresh, 6)):
            os.utime(path, ((now - timedelta(days=days)).timestamp(),) * 2)
        for directory in (nested.parent, nested.parent.parent, unit, stale.parent):
            os.utime(directory, ((now - timedelta(days=8)).timestamp(),) * 2)
        models.register_artifact(
            integration="workspace", type="file", external_id=f"worker-{retention}",
            parent_thread_id="worker", path=str(nested), retention=retention, db=db,
        )
        db.commit()
        parent_plan, delegated, _plan, _leaves, _watch, _schedule = _returned(
            db, "Write the tests",
        )
        submit_review_evidence(delegated.id, db=db, actor_id="parent")
        _verdict(db, parent_plan.id, "accept", delegated.id)
        db.commit()
        monkeypatch.setattr(thread_lifecycle_trims, "_entry_times", lambda st: st.st_mtime)

        report = run_retention_sweep(db, now=now, workspace=tmp_path)

        assert report.working_files_trimmed == 1
        assert nested.exists()
        assert fresh.exists()
        assert not stale.exists()
        assert db.execute(
            "SELECT state FROM artifact_identities WHERE external_id=?", (f"worker-{retention}",)
        ).fetchone()["state"] == "active"
    finally:
        db.close()


def test_cancelling_the_delegation_fails_the_workers_open_steps_and_retires_it(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, _plan, (leaf,), _watch, _schedule = _returned(
            db, "Write the tests",
        )
        result = mutate(
            parent_plan.id, "fail",
            {
                "node_id": delegated.id,
                "expected_revision": service._node_row(db, delegated.id)["revision"],
            },
            actor=PARENT, provenance="test.retirement", mutation_id=uuid.uuid4().hex, db=db,
        ).result
        db.commit()

        assert result["node"]["state"] == "failed"
        assert service.get_node(leaf.id, db=db).state == "failed"
        assert db.execute(
            "SELECT provenance FROM plan_node_events WHERE node_id=? "
            "AND event='state_transition' AND new_state='failed'",
            (leaf.id,),
        ).fetchone()["provenance"] == "plan.assignment_cancelled"
        assert review_evidence.latest(
            leaf.id, requirements_revision=1, db=db,
        )["evidence"] == "Write the tests done"
        assert _lifecycle(db, "worker") == ("archived", "assignment_settled")
    finally:
        db.close()


def test_closing_the_delegation_closes_the_workers_open_steps_and_retires_it(monkeypatch):
    """#1206: an idle worker's open steps never trap the owner's own plan.

    The step under a container is still being worked, not returned. Closing the
    delegation closes it and then the container above it -- closed, not
    failed, because nothing was attempted and lost -- and the worker retires.
    Every closed step stays on the worker's plan as the record (#1887).
    """
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, (returned,), _watch, _schedule = _returned(
            db, "Write the tests",
        )
        phase = service.add_node(
            plan.id, kind="phase", title="Ship", acceptance_criteria="Shipped", db=db,
        )
        working = service.add_node(
            plan.id, kind="task", title="Open the PR", acceptance_criteria="Open",
            parent_id=phase.id, db=db,
        )
        service.transition_node(working.id, "active", actor_id="worker", db=db)
        result = mutate(
            parent_plan.id, "close_node",
            {
                "node_id": delegated.id, "reason": "Superseded by the new approach",
                "expected_revision": service._node_row(db, delegated.id)["revision"],
            },
            actor=PARENT, provenance="test.retirement", mutation_id=uuid.uuid4().hex, db=db,
        ).result
        db.commit()

        assert result["node"]["state"] == "closed"
        for step in (delegated, returned, phase, working):
            row = service._node_row(db, step.id)
            assert (row["state"], row["archived_at"]) == ("closed", None)
        assert review_evidence.latest(
            returned.id, requirements_revision=1, db=db,
        )["evidence"] == "Write the tests done"
        assert _lifecycle(db, "worker") == ("archived", "assignment_settled")
    finally:
        db.close()


def test_a_durable_roster_owner_is_never_retired_by_a_verdict(monkeypatch):
    db = _db(monkeypatch, roster_worker=True)
    try:
        parent_plan, delegated, _plan, _leaves, _watch, _schedule = _returned(
            db, "Write the tests",
        )
        submit_review_evidence(delegated.id, db=db, actor_id="parent")

        _verdict(db, parent_plan.id, "accept", delegated.id)
        db.commit()

        assert _lifecycle(db, "worker") == ("active", None)
    finally:
        db.close()


def test_an_explicit_new_assignment_brings_a_retired_worker_back(monkeypatch):
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, _plan, _leaves, _watch, _schedule = _returned(
            db, "Write the tests",
        )
        submit_review_evidence(delegated.id, db=db, actor_id="parent")
        _verdict(db, parent_plan.id, "accept", delegated.id)
        db.commit()
        assert _lifecycle(db, "worker")[0] == "archived"

        again = service.add_node(
            parent_plan.id, kind="task", title="Delegate more work",
            acceptance_criteria="Reviewed", spawn_kind="claude-code", db=db,
        )
        service.transition_node(again.id, "active", db=db)
        mutations.attach_spawned_thread(again.id, "worker", db=db)
        db.commit()

        assert _lifecycle(db, "worker") == ("active", None)
    finally:
        db.close()


def test_a_rejection_does_not_retire_the_worker_that_holds_the_revision(monkeypatch):
    """The verdict fails the submission, but the worker's next work is the revision."""
    db = _db(monkeypatch)
    try:
        parent_plan, delegated, plan, leaves, watch, schedule = _returned(db, "Write the tests")

        mutate(
            parent_plan.id, "reject",
            {"node_id": delegated.id, "feedback": "Cover the empty body.",
             "expected_revision": service._node_row(db, delegated.id)["revision"]},
            actor=PARENT, provenance="test.rejection", mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()

        assert db.execute("SELECT archived_at FROM threads WHERE id='worker'").fetchone()[0] is None
        revision = db.execute(
            "SELECT spawned_thread_id, state FROM plan_nodes WHERE follow_up_of=?", (delegated.id,),
        ).fetchone()
        assert (revision["spawned_thread_id"], revision["state"]) == ("worker", "pending")
        # The returned step took the verdict; nothing else of the worker's was touched.
        assert service.get_node(leaves[0].id, db=db).state == "closed"
        assert watches.get_watch(watch.id, db=db).status == "armed"
        assert schedules.get(schedule.id, db=db).state != "cancelled"
    finally:
        db.close()


def test_a_nested_delegation_among_the_returned_steps_still_retires_its_own_worker(monkeypatch):
    """Only the rejected step's worker is kept for the revision; a nested worker settles."""
    db = _db(monkeypatch)
    try:
        db.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,agent_enabled,created_at,updated_at)
               VALUES ('subworker','user-1','project-1','worker','test/model',0,'t','t')""",
        )
        parent_plan = service.ensure_plan("thread", "parent", db=db)
        delegated = service.add_node(
            parent_plan.id, kind="task", title="Delegate work",
            acceptance_criteria="Worker result is reviewed", spawn_kind="claude-code", db=db,
        )
        service.transition_node(delegated.id, "active", db=db)
        plan = service.ensure_plan("thread", "worker", db=db)
        mutations.attach_spawned_thread(delegated.id, "worker", db=db)
        # The worker's own step was itself delegated to a sub-worker, then put up for review.
        nested = service.add_node(
            plan.id, kind="task", title="Nested", acceptance_criteria="Done",
            spawn_kind="claude-code", db=db,
        )
        service.transition_node(nested.id, "active", actor_id="worker", db=db)
        mutations.attach_spawned_thread(nested.id, "subworker", db=db)
        submit_review_evidence(nested.id, db=db, evidence="Nested done", actor_id="worker")
        db.commit()
        asyncio.run(runner.sweep(
            NOW, db=db, sources=[HarvestSource()], deliver=AsyncMock(return_value=True),
        ))
        assert service.get_node(delegated.id, db=db).state == "review_requested"

        mutate(
            parent_plan.id, "reject",
            {"node_id": delegated.id, "feedback": "Cover the empty body.",
             "expected_revision": service._node_row(db, delegated.id)["revision"]},
            actor=PARENT, provenance="test.rejection", mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()

        assert service.get_node(nested.id, db=db).state == "closed"
        assert db.execute("SELECT archived_at FROM threads WHERE id='subworker'").fetchone()[0] is not None
        assert db.execute("SELECT archived_at FROM threads WHERE id='worker'").fetchone()[0] is None
    finally:
        db.close()


def _accept_delegated_step_with_notes(db, *, parent_selects: bool) -> dict[str, bool]:
    """Accept a delegated step; report per thread whether its earlier taskless row left."""
    from plans import admission, admission_attempts, plan_write

    parent_plan = service.ensure_plan("thread", "parent", db=db)
    delegated = service.add_node(
        parent_plan.id, kind="task", title="Delegate work",
        acceptance_criteria="Worker result is reviewed",
        spawn_kind="claude-code", db=db,
    )
    notes = {}
    for thread_id in ("parent", "worker"):
        notes[thread_id] = models.register_artifact(
            integration="workspace", type="document",
            external_id=f"workspace://ws-1/.threads/{thread_id}/notes.md",
            parent_thread_id=thread_id, title="notes.md", retention="durable",
            metadata={"zone": "workspace"}, db=db,
        )["id"]
    db.execute("UPDATE artifact_registrations SET updated_at='2020-01-01T00:00:00'")
    db.commit()
    if parent_selects:
        # The production shape: the parent selects the step, then hands it to a worker.
        with plan_write.transaction(db) as connection:
            admission.activate(
                delegated.id, expected_revision=delegated.revision, actor_id="parent",
                provenance="test.retirement", db=connection,
            )
    else:
        service.transition_node(delegated.id, "active", db=db)
    plan = service.ensure_plan("thread", "worker", db=db)
    mutations.attach_spawned_thread(delegated.id, "worker", db=db)
    leaf = service.add_node(
        plan.id, kind="task", title="Write the tests", acceptance_criteria="Done", db=db,
    )
    with plan_write.transaction(db) as connection:
        admission.activate(
            leaf.id, expected_revision=leaf.revision, actor_id="worker",
            provenance="test.retirement", db=connection,
        )
    assert admission_attempts.executor_start(db, leaf.id)[0] == "worker"
    parent_start = admission_attempts.executor_start(db, delegated.id)
    assert (parent_start is not None and parent_start[0] == "parent") == parent_selects
    submit_review_evidence(leaf.id, db=db, evidence="done", actor_id="worker")
    asyncio.run(runner.sweep(
        NOW, db=db, sources=[HarvestSource()], deliver=AsyncMock(return_value=True),
    ))
    submit_review_evidence(delegated.id, db=db, actor_id="parent")

    _verdict(db, parent_plan.id, "accept", delegated.id)
    db.commit()

    assert service.get_node(leaf.id, db=db).state == "accepted"
    retired = {
        (row["artifact_id"], row["parent_thread_id"]): row["retired_at"] is not None
        for row in db.execute(
            "SELECT artifact_id,parent_thread_id,retired_at FROM artifact_registrations"
        ).fetchall()
    }
    return {thread_id: retired[(notes[thread_id], thread_id)] for thread_id in notes}


def test_accepting_a_delegated_step_the_parent_selected_retires_both_threads_taskless_rows(
    monkeypatch,
):
    db = _db(monkeypatch)
    try:
        assert _accept_delegated_step_with_notes(db, parent_selects=True) == {
            "parent": True, "worker": True,
        }
    finally:
        db.close()


def test_accepting_a_delegated_step_the_parent_never_selected_retires_only_the_workers_rows(
    monkeypatch,
):
    db = _db(monkeypatch)
    try:
        assert _accept_delegated_step_with_notes(db, parent_selects=False) == {
            "parent": False, "worker": True,
        }
    finally:
        db.close()
