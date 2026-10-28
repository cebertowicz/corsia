"""A taskless row leaves when its thread completes a task it started afterwards.

A row registered while its thread had no task selected has no origin task, so
task settlement never reaches it. Accepting a task retires the rows its
executor registered, and has not touched, since before it started that task.
A failed, closed or never-started task is not a completion and retires none.
"""

from __future__ import annotations

import logging

import pytest

from infra import config
from infra.database import get_db
from infra.platform_paths import WorkspaceSelection
from plans import admission, admission_attempts, plan_write, service as plans
from registry import models, node_retirement, pinning, taskless_row_backfill
from tests.plan_review_helpers import submit_review_evidence
from tests.registry_helpers import make_session

_PROJECT = "workspace://ws-1/projects/alpha"
# One stored format is 19 characters and the other 32; a text comparison
# across them is wrong, so the fixtures use the short one on purpose.
_BEFORE = "2020-01-01T00:00:00"
_AFTER = "2999-01-01T00:00:00.000000+00:00"


@pytest.fixture()
def db(isolated_db, tmp_path, monkeypatch):
    workspace = (tmp_path / "workspace").resolve()
    (workspace / "projects" / "alpha").mkdir(parents=True)
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(
        config, "WORKSPACE_SELECTION",
        WorkspaceSelection(workspace_id="ws-1", root=workspace, name="Test workspace"),
    )
    connection = get_db()
    connection.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES (?,?,?,'t0','t0')",
        ("alpha", "alpha", _PROJECT),
    )
    connection.commit()
    return connection


def _taskless(db, thread_id: str, name: str, *, touched: str = _BEFORE, **identity) -> dict:
    fields = {
        "integration": "workspace", "type": "document",
        "external_id": f"{_PROJECT}/.threads/t1/{name}",
        "retention": "durable", "metadata": {"zone": "workspace"},
    }
    fields.update(identity)
    artifact = models.register_artifact(
        parent_thread_id=thread_id, title=name, db=db, **fields,
    )
    db.execute(
        "UPDATE artifact_registrations SET updated_at=? "
        "WHERE artifact_id=? AND parent_thread_id=?",
        (touched, artifact["id"], thread_id),
    )
    db.commit()
    return artifact


def _started(db, thread_id: str, title: str = "Write"):
    """A task its thread selected: the start route that records an attempt."""
    plan = plans.ensure_plan("thread", thread_id, db=db)
    node = plans.add_node(
        plan.id, kind="task", title=title, acceptance_criteria="Done", db=db,
    )
    with plan_write.transaction(db) as connection:
        admission.activate(
            node.id, expected_revision=node.revision, actor_id=thread_id,
            provenance="test.taskless", db=connection,
        )
    assert admission_attempts.executor_start(db, node.id)[0] == thread_id
    return plans.get_node(node.id, db=db)


def _accept(db, node, thread_id: str) -> None:
    submit_review_evidence(node.id, db=db, actor_id=thread_id)
    plans.transition_node(
        node.id, "accepted", verification_result="passed", verified_by="reviewer", db=db,
    )
    db.commit()


def _rows(db, thread_id: str) -> dict[str, tuple[str, bool]]:
    return {
        row["artifact_id"]: (row["surfacing"], row["retired_at"] is not None)
        for row in db.execute(
            "SELECT artifact_id,surfacing,retired_at FROM artifact_registrations "
            "WHERE parent_thread_id=?",
            (thread_id,),
        ).fetchall()
    }


def test_accepting_a_task_retires_the_rows_registered_before_it_started(db):
    thread_id = make_session(db, prefix="rule3")
    other_thread = make_session(db, prefix="rule3-other")
    old = _taskless(db, thread_id, "old.md")
    pinned = _taskless(db, thread_id, "pinned.md")
    pinning.update_pinned(pinned["id"], pinned=True, parent_thread_id=thread_id, db=db)
    db.execute(
        "UPDATE artifact_registrations SET updated_at=? WHERE artifact_id=?",
        (_BEFORE, pinned["id"]),
    )
    shared = _taskless(
        db, thread_id, "REFERENCE.md",
        external_id=f"{_PROJECT}/shared/documents/REFERENCE.md",
        # "Shared" is read from the stored location, as registration writes it.
        path=str(config.WORKSPACE / "projects/alpha/shared/documents/REFERENCE.md"),
    )
    session = _taskless(
        db, thread_id, "session", integration="claude-code", type="session",
        external_id="session-1", retention="ephemeral", metadata={},
    )
    elsewhere = _taskless(db, other_thread, "theirs.md")
    node = _started(db, thread_id)
    touched_since = _taskless(db, thread_id, "during.md", touched=_AFTER)

    _accept(db, node, thread_id)

    rows = _rows(db, thread_id)
    assert rows[old["id"]] == ("settled", True)
    # A shared document's row is task-scoped like any other.
    assert rows[shared["id"]] == ("settled", True)
    # Pin keeps the row in the pane; the stamp records that it would have left.
    assert rows[pinned["id"]] == ("visible", True)
    for kept in (session, touched_since):
        assert rows[kept["id"]] == ("visible", False)
    assert _rows(db, other_thread)[elsewhere["id"]] == ("visible", False)
    event = db.execute(
        "SELECT event_type, authority, metadata FROM artifact_registration_events "
        "WHERE artifact_id=? ORDER BY rowid DESC LIMIT 1",
        (old["id"],),
    ).fetchone()
    assert event["event_type"] == "taskless_row_retired"
    assert event["authority"] == "plan-node-settled"
    assert "later_task_accepted" in event["metadata"]
    assert db.execute(
        "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='artifact'"
    ).fetchone()[0] == 0


def test_registering_again_brings_a_retired_row_back(db):
    thread_id = make_session(db, prefix="rule3-back")
    old = _taskless(db, thread_id, "old.md")
    _accept(db, _started(db, thread_id), thread_id)
    assert _rows(db, thread_id)[old["id"]] == ("settled", True)

    models.register_artifact(
        integration="workspace", type="document",
        external_id=f"{_PROJECT}/.threads/t1/old.md", parent_thread_id=thread_id,
        title="old.md", retention="durable", metadata={"zone": "workspace"}, db=db,
    )

    assert _rows(db, thread_id)[old["id"]] == ("visible", False)
    # The row now counts as registered at the re-registration, so the task
    # that retired it cannot retire it a second time.
    assert node_retirement.retire_taskless_rows(
        db, thread_id=thread_id, trigger="test",
        started_at=admission_attempts.latest_accepted_starts(db)[thread_id],
    ) == 0


def test_a_task_sent_back_for_revision_keeps_its_first_start(db):
    thread_id = make_session(db, prefix="rule3-first")
    node = _started(db, thread_id)
    first = admission_attempts.executor_start(db, node.id)
    assert first[0] == thread_id
    with plan_write.transaction(db) as connection:
        assert admission.release(node.id, "test_revision", db=connection)
        second, _conflict = admission.select(
            node.id, actor_id=thread_id, provenance="test.taskless", db=connection,
        )
    attempts = db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE node_id=?", (node.id,),
    ).fetchone()[0]
    assert second is not None and attempts == 2

    assert admission_attempts.executor_start(db, node.id) == first


def test_a_thread_the_task_has_left_keeps_its_rows(db):
    thread_id = make_session(db, prefix="rule3-runs")
    left = make_session(db, prefix="rule3-left")
    theirs = _taskless(db, left, "theirs.md")
    mine = _taskless(db, thread_id, "mine.md")
    node = _started(db, thread_id)
    # An older attempt by a thread that no longer runs the task.
    db.execute(
        "INSERT INTO plan_node_attempts (id,plan_id,node_id,executor_thread_id,"
        "requirements_revision,attempt_ordinal,provenance,selected_at,released_at,"
        "release_reason) VALUES ('left-1',?,?,?,1,99,'test.taskless',"
        "'2021-01-01T00:00:00','2021-01-02T00:00:00','executor_changed')",
        (node.plan_id, node.id, left),
    )
    db.commit()

    _accept(db, node, thread_id)

    assert _rows(db, thread_id)[mine["id"]] == ("settled", True)
    assert _rows(db, left)[theirs["id"]] == ("visible", False)
    # The one-time pass reads the same rule.
    assert set(admission_attempts.latest_accepted_starts(db)) == {thread_id}


def _register_again(db, thread_id: str, artifact: dict) -> None:
    models.register_artifact(
        integration="workspace", type="document",
        external_id=f"{_PROJECT}/.threads/t1/touched.md", parent_thread_id=thread_id,
        title="touched.md", retention="durable", metadata={"zone": "workspace"}, db=db,
    )


def _hide(db, thread_id: str, artifact: dict) -> None:
    models.update_surfacing(
        artifact["id"], surfacing="quiet", parent_thread_id=thread_id, db=db,
    )


def _pin(db, thread_id: str, artifact: dict) -> None:
    pinning.update_pinned(artifact["id"], pinned=True, parent_thread_id=thread_id, db=db)


def _unpin(db, thread_id: str, artifact: dict) -> None:
    pinning.update_pinned(artifact["id"], pinned=False, parent_thread_id=thread_id, db=db)


@pytest.mark.parametrize(
    ("before_start", "touch"),
    [(None, _register_again), (None, _hide), (None, _pin), (_pin, _unpin)],
    ids=["registered again", "hidden", "pinned", "unpinned"],
)
def test_every_write_to_a_row_after_the_task_started_counts_as_a_touch(
    db, before_start, touch,
):
    """The rule reads ``updated_at``: each writer of a row must advance it."""
    thread_id = make_session(db, prefix="rule3-touch")
    row = _taskless(db, thread_id, "touched.md")
    if before_start is not None:
        before_start(db, thread_id, row)
        db.execute(
            "UPDATE artifact_registrations SET updated_at=? WHERE artifact_id=?",
            (_BEFORE, row["id"]),
        )
        db.commit()
    node = _started(db, thread_id)

    touch(db, thread_id, row)
    db.commit()
    _accept(db, node, thread_id)

    assert _rows(db, thread_id)[row["id"]][1] is False


@pytest.mark.parametrize("outcome", ["failed", "closed", "accepted_unstarted"])
def test_a_task_that_is_not_a_completion_by_its_executor_retires_nothing(
    db, monkeypatch, outcome,
):
    thread_id = make_session(db, prefix="rule3-none")
    old = _taskless(db, thread_id, "old.md")
    calls: list[dict] = []
    monkeypatch.setattr(
        node_retirement, "retire_taskless_rows",
        lambda _db, **kwargs: calls.append(kwargs) or 0,
    )
    if outcome == "accepted_unstarted":
        # Accepted without its thread ever selecting it: no attempt exists.
        plan = plans.ensure_plan("thread", thread_id, db=db)
        node = plans.add_node(
            plan.id, kind="task", title="Never started", acceptance_criteria="Done", db=db,
        )
        plans.transition_node(node.id, "active", actor_id="user", db=db)
        assert admission_attempts.executor_start(db, node.id) is None
        _accept(db, node, "user")
    else:
        node = _started(db, thread_id)
        plans.transition_node(node.id, outcome, actor_id=thread_id, db=db)
    db.commit()

    assert calls == []
    assert _rows(db, thread_id)[old["id"]] == ("visible", False)


def _shared(db, thread_id: str, name: str, **kwargs) -> dict:
    return _taskless(
        db, thread_id, name, external_id=f"{_PROJECT}/shared/documents/{name}",
        path=str(config.WORKSPACE / "projects/alpha/shared/documents" / name), **kwargs,
    )


def test_the_one_time_pass_applies_the_rule_to_tasks_already_accepted(db, monkeypatch):
    from infra.settings import get_setting, set_setting
    from registry import taskless_row_backfill as backfill

    # The pass already ran once on this instance, under the earlier rule.
    set_setting("artifact_pane:taskless_rows_retired", {"completed_at": "t0"}, db=db)
    done_thread = make_session(db, prefix="rule3-pass")
    idle_thread = make_session(db, prefix="rule3-idle")
    with monkeypatch.context() as patched:
        # The task was accepted before the rule existed.
        patched.setattr(node_retirement, "retire_taskless_rows", lambda _db, **_kwargs: 0)
        left_behind = _taskless(db, done_thread, "old.md")
        shared = _shared(db, done_thread, "REFERENCE.md")
        _accept(db, _started(db, done_thread), done_thread)
    newer = _taskless(db, done_thread, "newer.md", touched=_AFTER)
    shared_newer = _shared(db, done_thread, "NEWER.md", touched=_AFTER)
    never_ran = _taskless(db, idle_thread, "idle.md")
    idle_shared = _shared(db, idle_thread, "IDLE.md")
    assert _rows(db, done_thread)[left_behind["id"]] == ("visible", False)

    backfill.run_startup_backfill()

    fresh = get_db()
    assert _rows(fresh, done_thread) == {
        left_behind["id"]: ("settled", True), shared["id"]: ("settled", True),
        newer["id"]: ("visible", False), shared_newer["id"]: ("visible", False),
    }
    assert _rows(fresh, idle_thread) == {
        never_ran["id"]: ("visible", False), idle_shared["id"]: ("visible", False),
    }
    marker = get_setting(backfill._MARKER, db=fresh)
    assert marker["retired_rows"] == 2
    assert marker["retired_by_thread"] == {done_thread: 2}
    event = fresh.execute(
        "SELECT metadata FROM artifact_registration_events "
        "WHERE artifact_id=? ORDER BY rowid DESC LIMIT 1",
        (left_behind["id"],),
    ).fetchone()
    assert "taskless_backfill" in event["metadata"]
    assert backfill.retire_taskless_rows(fresh) == {}
    assert fresh.execute(
        "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='artifact'"
    ).fetchone()[0] == 0


def _earlier_attempt(db, node, thread_id: str, selected_at: str, ordinal: int = 99) -> None:
    """An attempt the thread released before the one that completed the task.

    Attempt rows cannot be edited, so a start in a chosen format is added.
    """
    db.execute(
        "INSERT INTO plan_node_attempts (id,plan_id,node_id,executor_thread_id,"
        "requirements_revision,attempt_ordinal,provenance,selected_at,released_at,"
        "release_reason) VALUES (?,?,?,?,1,?,'test.taskless',?,"
        "'2021-12-31T00:00:00','test_revision')",
        (f"earlier-{node.id}-{ordinal}", node.plan_id, node.id, thread_id, ordinal,
         selected_at),
    )
    db.commit()


def test_the_latest_start_is_read_across_tasks_and_both_timestamp_formats(db):
    thread_id = make_session(db, prefix="rule3-several")
    tasks = []
    for title in ("One", "Two"):
        task = _started(db, thread_id, title)
        _accept(db, task, thread_id)
        # The helper accepts without the settlement route that frees the slot.
        with plan_write.transaction(db) as connection:
            assert admission.release(task.id, "test_settled", db=connection)
        tasks.append(task)
    one, two = tasks
    # Each task's start is its first attempt; one is stored in each format.
    _earlier_attempt(db, one, thread_id, "2021-06-01T00:00:00.000000+00:00")
    _earlier_attempt(db, two, thread_id, "2021-06-01T09:00:00")
    # A task still open is not a completion, however late it started.
    _started(db, thread_id, "Three")

    assert admission_attempts.latest_accepted_starts(db) == {
        thread_id: "2021-06-01T09:00:00",
    }


def test_a_task_with_a_start_that_is_not_a_time_is_not_counted(db, monkeypatch, caplog):
    bad_thread = make_session(db, prefix="rule3-bad")
    good_thread = make_session(db, prefix="rule3-good")
    theirs = _taskless(db, good_thread, "theirs.md")
    mine = _taskless(db, bad_thread, "mine.md")
    bad = _started(db, bad_thread, "Bad")
    good = _started(db, good_thread, "Good")
    with monkeypatch.context() as patched:
        # Both tasks were accepted before the rule existed.
        patched.setattr(node_retirement, "retire_taskless_rows", lambda _db, **_kwargs: 0)
        _accept(db, bad, bad_thread)
        _accept(db, good, good_thread)
    readable = admission_attempts.latest_accepted_starts(db)
    assert set(readable) == {bad_thread, good_thread}
    _earlier_attempt(db, bad, bad_thread, "not a time")

    with caplog.at_level(logging.WARNING, logger=admission_attempts.__name__):
        starts = admission_attempts.latest_accepted_starts(db)
        retired = taskless_row_backfill.retire_taskless_rows(db)

    # The task's first start is unknown, so its later, readable attempt does not
    # stand in for it: the task is left out and its thread's row stays.
    assert starts == {good_thread: readable[good_thread]}
    assert "unreadable start 'not a time'" in caplog.text
    assert retired == {good_thread: 1}
    assert _rows(db, good_thread)[theirs["id"]] == ("settled", True)
    assert _rows(db, bad_thread)[mine["id"]][0] == "visible"


def test_accepting_a_task_with_a_start_that_is_not_a_time_retires_nothing(db, caplog):
    thread_id = make_session(db, prefix="rule3-live-bad")
    mine = _taskless(db, thread_id, "mine.md")
    task = _started(db, thread_id, "Bad")
    assert admission_attempts.executor_start(db, task.id) is not None
    _earlier_attempt(db, task, thread_id, "not a time")

    with caplog.at_level(logging.WARNING, logger=admission_attempts.__name__):
        assert admission_attempts.executor_start(db, task.id) is None
        _accept(db, task, thread_id)

    # Settlement and the startup pass read the same unknown start the same way.
    assert "unreadable start; start unknown" in caplog.text
    assert _rows(db, thread_id)[mine["id"]][0] == "visible"
    assert thread_id not in admission_attempts.latest_accepted_starts(db)


def test_an_unreadable_task_does_not_hide_the_threads_other_tasks(db):
    thread_id = make_session(db, prefix="rule3-mixed")
    for title in ("Bad", "Good"):
        task = _started(db, thread_id, title)
        _accept(db, task, thread_id)
        with plan_write.transaction(db) as connection:
            assert admission.release(task.id, "test_settled", db=connection)
        if title == "Bad":
            _earlier_attempt(db, task, thread_id, "not a time")
        else:
            _earlier_attempt(db, task, thread_id, "2021-06-01T09:00:00")

    assert admission_attempts.latest_accepted_starts(db) == {thread_id: "2021-06-01T09:00:00"}
