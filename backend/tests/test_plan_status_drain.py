"""A plan write's status is journaled after commit, off the writer lock.

The write marks its plan dirty in its own transaction; the drain computes the
thin projection in a read snapshot and appends it in a short write, only if the
marker still carries the seq it read. The owner's digest drains whatever is
still owed first, and startup sweeps what a process exit left.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

CLOCK = "2026-10-08T12:00:00+00:00"

#: Today's projections for the scene below, in journal order. ``{plan}`` and
#: ``{first}`` are the scene's generated ids; nothing else varies.
_IDLE = (
    'Current plan status: {{"as_of":"{clock}","current_task":null,"next_step":null,'
    '"no_current_task":true,"note":"idle: nothing open","plan":{{"id":"{plan}","revision":1,'
    '"state":"active"}},"progress":{{"done":0,"liveness":"idle","total":0}},"severity":"idle",'
    '"status_read":"plan_read()"}}'
)
_PENDING = (
    'Current plan status: {{"as_of":"{clock}","current_task":null,"next_step":{{"condition":"ready",'
    '"detail_read":"plan_read(node_id=\'{first}\')","id":"{first}","reason":"Not started",'
    '"revision":1,"state":"pending","title":"First"}},"no_current_task":true,"plan":{{"id":"{plan}",'
    '"revision":{revision},"state":"active"}},"progress":{{"done":0,"liveness":"idle","total":{total}}},'
    '"severity":"ready","status_read":"plan_read()"}}'
)
_ACTIVE = (
    'Current plan status: {{"as_of":"{clock}","current_task":null,"next_step":{{"condition":"ready",'
    '"detail_read":"plan_read(node_id=\'{first}\')","id":"{first}","reason":"Set aside",'
    '"revision":2,"state":"active","title":"First"}},"no_current_task":true,"plan":{{"id":"{plan}",'
    '"revision":{revision},"state":"active"}},"progress":{{"done":0,"liveness":"idle","total":{total}}},'
    '"severity":"ready","status_read":"plan_read()"}}'
)


def _row(scene, template: str, revision: int, total: int = 0) -> tuple[str, str, str]:
    summary = template.format(
        clock=CLOCK, plan=scene.plan, first=scene.first, revision=revision, total=total,
    )
    details = f'{{"chip": null, "path": "plan.status", "plan_revision": {revision}}}'
    return scene.plan, summary, details


def _through_second_add(scene) -> list[tuple[str, str, str]]:
    """The rows journaled by ensure_plan, add First, add Second."""
    return [
        _row(scene, _IDLE, 1),
        _row(scene, _PENDING, 2, 1),
        _row(scene, _PENDING, 3, 2),
    ]


@pytest.fixture
def owner(isolated_db, monkeypatch):
    """A file database with one owner thread and a frozen clock."""
    from infra.database import get_db

    monkeypatch.setattr("plans.service._now", lambda: CLOCK)
    monkeypatch.setattr("infra.passive_changes.utc_now_iso", lambda: CLOCK)
    monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
    db = get_db()
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t0','t0')"
    )
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at) "
        "VALUES ('owner','user','project','native/model','t0','t0')"
    )
    db.commit()
    return db


@pytest.fixture
def scene(owner):
    """A plan with two pending tasks, each write's status already drained."""
    from types import SimpleNamespace

    from plans import service

    plan = service.ensure_plan("thread", "owner", db=owner)
    first = service.add_node(plan.id, kind="task", title="First", acceptance_criteria="One", db=owner)
    service.add_node(plan.id, kind="task", title="Second", acceptance_criteria="Two", db=owner)
    return SimpleNamespace(plan=plan.id, first=first.id, path=str(owner.execute(
        "PRAGMA database_list").fetchone()["file"]))


def _status_rows(db) -> list[tuple]:
    return [
        tuple(row)
        for row in db.execute(
            "SELECT object_id,summary,details_json FROM context_change_journal "
            "WHERE change_kind='plan.status' ORDER BY seq"
        )
    ]


def _markers(db) -> list[str]:
    return [str(row[0]) for row in db.execute("SELECT plan_id FROM plan_status_dirty")]


def _digest(db, thread_id: str = "owner") -> str:
    from infra.passive_changes import materialize_pending_digest

    accepted = materialize_pending_digest(db, thread_id)
    db.commit()
    return db.execute(
        "SELECT content FROM messages WHERE id=?", (accepted.message_id,)
    ).fetchone()["content"]


def _second_connection(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def test_kill_between_commit_and_drain_leaves_the_marker_for_the_startup_sweep(
    owner, scene, monkeypatch,
):
    from plans import service, status_drain

    with monkeypatch.context() as patched:
        def exit_process(*_args):
            raise SystemExit("killed between commit and drain")

        patched.setattr(status_drain, "drain", exit_process)
        with pytest.raises(SystemExit):
            service.transition_node(scene.first, "active", db=owner)

    assert service.get_node(scene.first, db=owner).state == "active"
    assert _markers(owner) == [scene.plan]
    assert _status_rows(owner) == _through_second_add(scene)

    status_drain.sweep(owner)

    assert _markers(owner) == []
    assert _status_rows(owner) == [*_through_second_add(scene), _row(scene, _ACTIVE, 4, 2)]


def test_kill_between_commit_and_drain_is_journaled_by_the_next_digest(
    owner, scene, monkeypatch,
):
    from plans import service, status_drain

    with monkeypatch.context() as patched:
        def exit_process(*_args):
            raise SystemExit("killed between commit and drain")

        patched.setattr(status_drain, "drain", exit_process)
        with pytest.raises(SystemExit):
            service.transition_node(scene.first, "active", db=owner)

    content = _digest(owner)

    assert content == "[PASSIVE CONTEXT DIGEST]\n" + _row(scene, _ACTIVE, 4, 2)[1]
    assert _markers(owner) == []


def test_a_failing_drain_leaves_the_marker_and_the_next_digest_is_current(
    owner, scene, monkeypatch,
):
    from plans import service, status_context

    with monkeypatch.context() as patched:
        def broken(*_args, **_kwargs):
            raise RuntimeError("projection failed")

        patched.setattr(status_context, "compute_status", broken)
        service.transition_node(scene.first, "active", db=owner)

    assert _markers(owner) == [scene.plan]
    assert _status_rows(owner) == _through_second_add(scene)

    content = _digest(owner)

    assert content == "[PASSIVE CONTEXT DIGEST]\n" + _row(scene, _ACTIVE, 4, 2)[1]
    assert _markers(owner) == []


def test_a_write_during_the_drain_leaves_the_plan_dirty_for_the_next_drain(
    owner, scene, monkeypatch,
):
    from plans import service, status_context, status_drain

    other = _second_connection(scene.path)
    real_compute = status_context.compute_status
    real_schedule = status_drain.schedule
    calls: list[int] = []

    def compute_then_write_elsewhere(db, row, **kwargs):
        computed = real_compute(db, row, **kwargs)
        if not calls:
            calls.append(1)
            # The other writer's own drain has not run yet.
            monkeypatch.setattr(status_drain, "schedule", lambda *_args: None)
            service.add_node(scene.plan, kind="task", title="Third", acceptance_criteria="Three", db=other)
            monkeypatch.setattr(status_drain, "schedule", real_schedule)
        return computed

    monkeypatch.setattr(status_context, "compute_status", compute_then_write_elsewhere)
    service.transition_node(scene.first, "active", db=owner)

    # The drain read seq S, the other write replaced it: nothing appended.
    assert _status_rows(owner) == _through_second_add(scene)
    assert _markers(owner) == [scene.plan]

    status_drain.sweep(owner)

    assert _status_rows(owner) == [*_through_second_add(scene), _row(scene, _ACTIVE, 5, 3)]
    assert _markers(owner) == []
    other.close()


def test_the_digest_after_a_drain_is_byte_equal_and_computed_off_the_writer_lock(
    owner, scene, monkeypatch,
):
    from plans import service, status_context

    real_compute = status_context.compute_status
    lock_free: list[bool] = []

    def probe_lock(db, row, **kwargs):
        probe = _second_connection(scene.path)
        probe.execute("PRAGMA busy_timeout=0")
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
            lock_free.append(True)
        except sqlite3.OperationalError:
            lock_free.append(False)
        finally:
            probe.close()
        return real_compute(db, row, **kwargs)

    monkeypatch.setattr(status_context, "compute_status", probe_lock)
    service.transition_node(scene.first, "active", db=owner)

    assert lock_free == [True]
    assert _markers(owner) == []
    assert _status_rows(owner) == [*_through_second_add(scene), _row(scene, _ACTIVE, 4, 2)]
    # Captured from the in-write journal before this change, same mutations.
    assert _digest(owner) == "[PASSIVE CONTEXT DIGEST]\n" + _row(scene, _ACTIVE, 4, 2)[1]


def _wake(connection, ordinal: int = 1) -> None:
    from plans import plan_wake

    plan_wake.append_input(
        connection,
        recipient="owner",
        message_id=f"plan-wake:test:{ordinal}",
        content="Wake",
        metadata={"event_type": "plan.work_selected"},
    )


def _latest_digest(db) -> str:
    return db.execute(
        "SELECT content FROM messages WHERE id LIKE 'context-digest:owner:%' ORDER BY rowid DESC"
    ).fetchone()["content"]


def _count_computes(monkeypatch) -> list[str]:
    from plans import status_context

    real_compute = status_context.compute_status
    computed: list[str] = []

    def counted(db, row, **kwargs):
        computed.append(str(row["id"]))
        return real_compute(db, row, **kwargs)

    monkeypatch.setattr(status_context, "compute_status", counted)
    return computed


def test_a_wake_inside_a_plan_write_delivers_the_new_status(owner, scene, monkeypatch):
    from plans import plan_write, service

    computed = _count_computes(monkeypatch)
    with plan_write.transaction(owner) as connection:
        service.transition_node(scene.first, "active", db=connection)
        _wake(connection)

    assert _latest_digest(owner) == "[PASSIVE CONTEXT DIGEST]\n" + _row(scene, _ACTIVE, 4, 2)[1]
    assert _status_rows(owner) == [*_through_second_add(scene), _row(scene, _ACTIVE, 4, 2)]
    # The commit's drain recomputes off the lock and finds nothing new to append.
    assert computed == [scene.plan, scene.plan]
    assert _markers(owner) == []


def test_a_write_after_the_in_write_digest_is_drained_after_commit(owner, scene):
    from plans import plan_write, service

    with plan_write.transaction(owner) as connection:
        service.transition_node(scene.first, "active", db=connection)
        _wake(connection)
        service.add_node(scene.plan, kind="task", title="Third", acceptance_criteria="Three",
                         db=connection)

    assert _latest_digest(owner) == "[PASSIVE CONTEXT DIGEST]\n" + _row(scene, _ACTIVE, 4, 2)[1]
    assert _status_rows(owner) == [
        *_through_second_add(scene), _row(scene, _ACTIVE, 4, 2), _row(scene, _ACTIVE, 5, 3),
    ]
    assert _markers(owner) == []


def test_a_watch_armed_after_the_in_write_digest_is_journaled_after_commit(owner, scene):
    """A change to what the plan waits on reaches the journal with no write to the plan's rows."""
    from plans import plan_write, service, status_context

    with plan_write.transaction(owner) as connection:
        service.transition_node(scene.first, "active", db=connection)
        _wake(connection)
        connection.execute(
            """INSERT INTO expected_event_watches
               (id,owner_thread_id,plan_id,plan_node_id,condition_type,condition_value,
                armed_at,created_at,updated_at)
               VALUES ('watch','owner',?,?,'deadline','2026-10-09T06:00:00+00:00',?,?,?)""",
            (scene.plan, scene.first, CLOCK, CLOCK, CLOCK),
        )

    assert _latest_digest(owner) == "[PASSIVE CONTEXT DIGEST]\n" + _row(scene, _ACTIVE, 4, 2)[1]
    plan = owner.execute("SELECT * FROM plans WHERE id=?", (scene.plan,)).fetchone()
    committed, _revision = status_context.compute_status(owner, plan)
    assert committed != _row(scene, _ACTIVE, 4, 2)[1]
    assert _status_rows(owner)[-1][1] == committed
    assert _markers(owner) == []


def test_fifo_seqs_are_assigned_in_the_admitting_write_and_the_drain_takes_none(owner, scene):
    """The after-commit drain appends journal rows only; it never admits an input."""
    from plans import plan_write, service

    def next_order() -> int:
        return int(owner.execute(
            "SELECT next_input_order FROM threads WHERE id='owner'"
        ).fetchone()[0])

    before = next_order()
    statements: list[str] = []
    owner.set_trace_callback(lambda text: statements.append(" ".join(text.upper().split())))
    try:
        with plan_write.transaction(owner) as connection:
            service.transition_node(scene.first, "active", db=connection)
            _wake(connection)
    finally:
        owner.set_trace_callback(None)

    commit = statements.index("COMMIT")
    takes_seq = [
        index for index, text in enumerate(statements)
        if "SET NEXT_INPUT_ORDER=NEXT_INPUT_ORDER+1" in text
    ]
    # The digest and the wake each take their seq inside the plan write.
    assert len(takes_seq) == 2 and max(takes_seq) < commit
    assert any(text.startswith("DELETE FROM PLAN_STATUS_DIRTY") for text in statements[commit + 1:])
    assert not [
        text for text in statements[commit + 1:]
        if "NEXT_INPUT_ORDER" in text or text.startswith("INSERT INTO MESSAGES")
    ]
    assert next_order() == before + 2
    assert [tuple(row) for row in owner.execute(
        "SELECT id,input_order FROM messages WHERE thread_id='owner' AND input_order>=? "
        "ORDER BY input_order", (before,),
    )] == [
        (owner.execute(
            "SELECT id FROM messages WHERE id LIKE 'context-digest:owner:%'"
        ).fetchone()[0], before),
        ("plan-wake:test:1", before + 1),
    ]


def test_a_rolled_back_in_write_digest_leaves_the_status_owed(owner, scene):
    from infra.database import write_transaction
    from plans import plan_write, service

    with plan_write.transaction(owner) as connection:
        service.transition_node(scene.first, "active", db=connection)
        with pytest.raises(RuntimeError):
            with write_transaction(connection, scope="wake") as nested:
                _wake(nested)
                raise RuntimeError("delivery failed")

    assert owner.execute(
        "SELECT COUNT(*) FROM messages WHERE id LIKE 'context-digest:owner:%'"
    ).fetchone()[0] == 0
    # The append rolled back with the digest; the commit's drain journals it.
    assert _status_rows(owner) == [*_through_second_add(scene), _row(scene, _ACTIVE, 4, 2)]
    assert _markers(owner) == []


def test_a_drain_racing_a_second_write_leaves_the_plan_dirty_until_its_own_drain(
    owner, scene, monkeypatch,
):
    from infra.database import get_db
    from plans import service, status_context

    real_compute = status_context.compute_status
    first_computed, first_go, second_committed, first_done = (threading.Event() for _ in range(4))
    failures: list[BaseException] = []

    def gated(db, row, **kwargs):
        name = threading.current_thread().name
        if name == "second-writer":
            second_committed.set()
            assert first_done.wait(timeout=5)
            return real_compute(db, row, **kwargs)
        computed = real_compute(db, row, **kwargs)
        if name == "first-writer":
            first_computed.set()
            assert first_go.wait(timeout=5)
        return computed

    monkeypatch.setattr(status_context, "compute_status", gated)

    def run(action):
        def body():
            try:
                action(get_db())
            except BaseException as error:  # surfaced below
                failures.append(error)
        return body

    first = threading.Thread(
        target=run(lambda db: service.transition_node(scene.first, "active", db=db)),
        name="first-writer",
    )
    second = threading.Thread(
        target=run(lambda db: service.add_node(
            scene.plan, kind="task", title="Third", acceptance_criteria="Three", db=db,
        )),
        name="second-writer",
    )
    first.start()
    assert first_computed.wait(timeout=5)
    second.start()
    assert second_committed.wait(timeout=5)
    first_go.set()
    first.join(timeout=5)

    assert not first.is_alive()
    assert _status_rows(owner) == _through_second_add(scene)
    assert _markers(owner) == [scene.plan]

    first_done.set()
    second.join(timeout=5)

    assert not second.is_alive()
    assert failures == []
    assert _status_rows(owner) == [*_through_second_add(scene), _row(scene, _ACTIVE, 5, 3)]
    assert _markers(owner) == []


def test_a_write_that_leaves_the_projection_unchanged_appends_nothing(owner, scene):
    from plans import plan_write

    with plan_write.transaction(owner) as connection:
        connection.execute("UPDATE plans SET updated_at=? WHERE id=?", (CLOCK, scene.plan))

    assert _status_rows(owner) == _through_second_add(scene)
    assert _markers(owner) == []


def test_the_drain_reads_no_cost(owner, scene, monkeypatch):
    """The thin status shows no cost, so a write's drain never reads the usage ledger (#2489)."""
    from plans import operator_node_facts, service

    real = operator_node_facts.project_costs
    wanted: list[bool] = []

    def recording(node_plans, **kwargs):
        wanted.append(kwargs.get("wanted", True))
        return real(node_plans, **kwargs)

    monkeypatch.setattr(operator_node_facts, "project_costs", recording)
    service.add_node(scene.plan, kind="task", title="Third", acceptance_criteria="Three", db=owner)

    assert wanted and not any(wanted)


def test_the_migration_adds_the_marker_table_to_an_existing_database():
    from contextlib import closing

    from infra.database import initialize_schema
    from infra.database_migrations import MIGRATIONS, migrate_to_current
    from infra.migration_98_to_99_plan_status_dirty import migrate_98_to_99
    from infra.schema import SCHEMA_VERSION

    assert MIGRATIONS[98] is migrate_98_to_99
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        db.execute("DROP TABLE plan_status_dirty")
        db.execute("PRAGMA user_version=98")
        db.commit()

        migrate_to_current(db, SCHEMA_VERSION)

        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        db.execute("INSERT OR REPLACE INTO plan_status_dirty (plan_id) VALUES ('p')")
        db.execute("INSERT OR REPLACE INTO plan_status_dirty (plan_id) VALUES ('p')")
        assert db.execute("SELECT dirty_seq, plan_id FROM plan_status_dirty").fetchall() == [(2, "p")]
