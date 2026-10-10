"""A plan write's work inside the writer lock does not grow with history (#1929, #1948).

Every plan write marks its owner's status owed before commit, and the status
is journaled after commit. Two things kept the lock held for seconds on a
long-lived plan when the status was journaled inside the write: a nested plan
write journaled once per level, and the cost reads loaded every usage row of the plan into
Python to sum it. In production the lock was still held for seconds (#2489):
the journaled status summed costs it never shows, and the wake-recipient read
scanned the whole messages index. Neither may happen under the lock.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from plans import (
    commitment_cost,
    node_cost,
    node_order,
    notifications,
    plan_write,
    projections,
    service,
    status_context,
    status_drain,
)
from infra import write_admission
from infra.database import write_transaction
from infra.turn_usage_remainder import pending_measurement_counts
from infra.schema import SCHEMA_SQL
from plans.boundary import mutate
from tests.plan_scene import (  # noqa: F401 - shared plan fixtures
    ACTOR,
    _mid,
    db,
    scene,
)
from tests.test_attention_queue import _seed
from tests.plan_writer_authorizer import raw_plan_seed


class _Fetched:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _RowCounter:
    """Count every row a read transfers out of SQLite."""

    def __init__(self, db):
        self._db = db
        self.rows = 0

    def execute(self, *args):
        rows = self._db.execute(*args).fetchall()
        self.rows += len(rows)
        return _Fetched(rows)


def _usage(db, node, count: int, *, start: int = 0) -> None:
    db.executemany(
        """INSERT INTO llm_usage (
               id,thread_id,turn_id,model,total_tokens,cost_usd,
               plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,
               plan_usage_role,source,created_at
           ) VALUES (?,'root','turn-1','test-model',?,?,?,?,'attempt-1',?,
                     'execution','test','t')""",
        [
            (f"usage-{index}", 10, None if index == 0 else 0.5,
             node.plan_id, node.id, node.commitment_id)
            for index in range(start, start + count)
        ],
    )
    db.commit()


def _costs(db, node) -> tuple[dict, dict, int]:
    counter = _RowCounter(db)
    own = node_cost.own_many([node.id], db=counter)[node.id]
    commitment = commitment_cost.project_many([node.commitment_id], db=counter)
    return own, commitment[node.commitment_id], counter.rows


def _record_marks(monkeypatch) -> list[str]:
    """Record every plan a write marks as owing its status, then mark it."""
    marked: list[str] = []
    real = status_drain.mark

    def record(connection, plan_ids):
        plan_ids = list(plan_ids)
        marked.extend(plan_ids)
        real(connection, plan_ids)

    monkeypatch.setattr(status_drain, "mark", record)
    return marked


def test_a_nested_plan_write_marks_its_status_once(db, scene, monkeypatch):
    journaled = _record_marks(monkeypatch)

    mutate(
        scene.plan_id, "edit", {
            "node_id": scene.pending,
            "expected_revision": service.get_node(scene.pending, db=db).revision,
            "description": "Edited",
        },
        actor=ACTOR, provenance="test", mutation_id=_mid(), db=db,
    )

    assert journaled == [scene.plan_id]


def _file_backed_connection(path):
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=1000")
    return connection


def _file_backed_plans(path):
    setup = _file_backed_connection(path)
    setup.executescript(SCHEMA_SQL)
    _seed(setup)
    plan_a = service.ensure_plan("thread", "root", db=setup)
    plan_b = service.ensure_plan("thread", "other-root", db=setup)
    setup.commit()
    setup.close()
    return plan_a, plan_b


def _wait_until_queued(ticket: write_admission.WriteTicket, *, timeout: float = 2.0) -> bool:
    """Wait until a writer is queued behind this ticket's holder."""
    deadline = time.monotonic() + timeout
    while not ticket._waiters:
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


def _contended_journal(path, plan_a, plan_b, monkeypatch):
    writer_a = _file_backed_connection(path)
    writer_b = _file_backed_connection(path)
    b_locked = threading.Event()
    b_releasing = threading.Event()
    failures: list[BaseException] = []
    journaled = _record_marks(monkeypatch)
    real_now = service._now

    def controlled_now():
        if threading.current_thread().name == "plan-writer-a":
            return (
                "9999-01-01T00:00:03+00:00"
                if b_releasing.is_set()
                else "9999-01-01T00:00:01+00:00"
            )
        return real_now()

    monkeypatch.setattr(service, "_now", controlled_now)

    def write_b():
        try:
            with write_transaction(writer_b, scope="other_writer") as connection:
                connection.execute(
                    "UPDATE plans SET updated_at=? WHERE id=?",
                    ("9999-01-01T00:00:02+00:00", plan_b.id),
                )
                b_locked.set()
                ticket = write_admission.ticket_of(connection)
                if ticket is None or not _wait_until_queued(ticket):
                    raise TimeoutError("plan writer never queued for the write lock")
                b_releasing.set()
        except BaseException as error:
            failures.append(error)

    def write_a():
        try:
            with plan_write.transaction(writer_a) as connection:
                connection.execute(
                    "UPDATE plans SET updated_at=? WHERE id=?",
                    ("9999-01-01T00:00:03+00:00", plan_a.id),
                )
        except BaseException as error:
            failures.append(error)

    other = threading.Thread(target=write_b, name="plan-writer-b")
    waiting = threading.Thread(target=write_a, name="plan-writer-a")
    other.start()
    assert b_locked.wait(timeout=2)
    waiting.start()
    other.join(timeout=2)
    waiting.join(timeout=2)
    assert not other.is_alive()
    assert not waiting.is_alive()
    writer_a.close()
    writer_b.close()
    return journaled, failures


def test_a_waiting_plan_write_does_not_mark_another_writers_plan(tmp_path, monkeypatch):
    """The touch cutoff starts only once this writer owns SQLite's lock."""
    plan_a, plan_b = _file_backed_plans(tmp_path / "plans.sqlite3")
    journaled, failures = _contended_journal(tmp_path / "plans.sqlite3", plan_a, plan_b, monkeypatch)

    assert failures == []
    assert journaled == [plan_a.id]


def test_a_plan_write_reads_no_cost_under_the_writer_lock(db, scene, monkeypatch):
    """#2489: the journaled status shows no cost, so neither its write nor its drain sums one."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("cost roll-up read inside a plan write")

    monkeypatch.setattr(commitment_cost, "project_many", refuse)
    monkeypatch.setattr(node_cost, "project_many", refuse)
    journaled: list[str] = []
    real = status_context.compute_status
    monkeypatch.setattr(
        status_context, "compute_status",
        lambda connection, row, **kwargs: (
            journaled.append(str(row["id"])), real(connection, row, **kwargs),
        )[1],
    )

    mutate(
        scene.plan_id, "edit", {
            "node_id": scene.pending,
            "expected_revision": service.get_node(scene.pending, db=db).revision,
            "description": "Edited",
        },
        actor=ACTOR, provenance="test", mutation_id=_mid(), db=db,
    )

    assert journaled == [scene.plan_id]


def test_wake_recipients_reads_only_rows_above_its_floor(db):
    """#2489: under the writer lock, the wake read seeks by rowid, never scans."""
    issued: list[tuple] = []

    class _Recording:
        def execute(self, sql, params=()):
            issued.append((sql, params))
            return db.execute(sql, params)

    notifications.wake_recipients(0, db=_Recording())

    (sql, params), = issued
    plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params)]
    assert plan == ["SEARCH messages USING INTEGER PRIMARY KEY (rowid>?)"]


def test_pending_measurements_read_only_turns_still_holding_receipts(db):
    """A commitment's owed measurements come from the partial index, not every Turn."""
    issued: list[tuple] = []

    class _Recording:
        def execute(self, sql, params=()):
            issued.append((sql, params))
            return db.execute(sql, params)

    pending_measurement_counts(_Recording(), ["commitment-a", "commitment-b"])

    (sql, params), = issued
    plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params)]
    assert plan[0] == "SCAN turns USING INDEX idx_active_turns_pending_usage"


def _mark_touched(db, since: str, monkeypatch) -> tuple[list[str], list[str]]:
    """Run mark_touched; return the plan ids it marked and its discovery query plan."""
    journaled: list[str] = []
    monkeypatch.setattr(
        status_drain, "mark",
        lambda _connection, plan_ids: journaled.extend(plan_ids),
    )
    issued: list[tuple] = []

    class _Recording:
        def execute(self, sql, params=()):
            issued.append((sql, params))
            return db.execute(sql, params)

    status_context.mark_touched(_Recording(), since=since)
    (sql, params), = issued
    plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params)]
    return journaled, plan


def test_touched_plans_are_found_without_probing_every_plan(db, scene, monkeypatch):
    """Under the writer lock, finding touched plans never probes each plan in turn.

    A correlated per-plan probe scanned plan_node_attempts once per plan and
    held the lock ~1.5 s per write on a 1,600-plan production database.
    """
    later = "9999-01-01T00:00:00+00:00"
    untouched, plan = _mark_touched(db, later, monkeypatch)
    assert untouched == []
    assert not any("CORRELATED" in step for step in plan)

    db.execute("UPDATE plan_nodes SET updated_at=? WHERE id=?", (later, scene.pending))
    touched, _plan = _mark_touched(db, later, monkeypatch)
    assert touched == [scene.plan_id]


def test_rollup_reuses_rows_without_changing_nested_rejected_or_stranded_status(
    db, scene, monkeypatch,
):
    """The status journal's supplied depth-first rows equal its former re-read.

    The rollup order differs from the global ``order_key,id`` order that
    ``projections`` formerly loaded. Cover both a rejected branch and a
    stranded row so progress, moving, and the owner digest stay exact.
    """
    parent = service.add_node(
        scene.plan_id, kind="phase", title="Parent", acceptance_criteria="done", db=db,
    )
    child = service.add_node(
        scene.plan_id, kind="task", title="Child", acceptance_criteria="done",
        parent_id=parent.id, db=db,
    )
    service.transition_node(child.id, "active", actor_id="root", db=db)
    rejected = service.add_node(
        scene.plan_id, kind="phase", title="Rejected", acceptance_criteria="done", db=db,
    )
    service.add_node(
        scene.plan_id, kind="task", title="Old round", acceptance_criteria="done",
        parent_id=rejected.id, db=db,
    )
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='closed', sent_back=1 WHERE id=?", (rejected.id,))
    db.execute(
        """INSERT INTO plan_node_events(id,node_id,event,provenance,payload_json,created_at)
           VALUES ('rejected-branch',?,'review_rejected','test','{}','t')""",
        (rejected.id,),
    )
    stranded_parent = service.add_node(
        scene.plan_id, kind="phase", title="Archived parent", acceptance_criteria="done", db=db,
    )
    stranded = service.add_node(
        scene.plan_id, kind="task", title="Stranded", acceptance_criteria="done",
        parent_id=stranded_parent.id, db=db,
    )
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (stranded_parent.id,))
    db.commit()

    row = service._plan_row(db, scene.plan_id)
    ordered = [item for item, _path in node_order.ordered_nodes_with_paths(db, scene.plan_id)]
    supplied = projections._plan_state(db, scene.plan_id, {}, plan=row, nodes=ordered)
    reloaded = projections.plan_state(scene.plan_id, db=db)
    assert supplied == reloaded

    optimized = status_context.projection(db, row, actor=ACTOR, clock="2026-10-04T00:00:00+00:00")
    original = projections._plan_state

    def without_supplied_rows(connection, plan_id, memo, *, plan=None, nodes=None):
        return original(connection, plan_id, memo)

    monkeypatch.setattr(projections, "_plan_state", without_supplied_rows)
    baseline = status_context.projection(db, row, actor=ACTOR, clock="2026-10-04T00:00:00+00:00")

    assert status_context.render(optimized) == status_context.render(baseline)
    assert optimized["progress"] == baseline["progress"]
    assert optimized["severity"] == baseline["severity"]


def test_cost_reads_do_not_grow_with_usage_rows(db, scene):
    node = service.get_node(scene.own, db=db)
    _usage(db, node, 3)
    own, commitment, few_rows = _costs(db, node)

    _usage(db, node, 30, start=3)
    grown_own, grown_commitment, many_rows = _costs(db, node)

    assert own["tokens"] == {"known": 30, "unknown_rows": 0}
    assert own["model_cost_usd"] == {"known": 1.0, "unknown_rows": 1}
    assert grown_own["tokens"] == {"known": 330, "unknown_rows": 0}
    assert grown_own["model_cost_usd"] == {"known": 16.0, "unknown_rows": 1}
    assert grown_commitment["tokens"] == grown_own["tokens"]
    assert grown_commitment["model_cost_usd"] == grown_own["model_cost_usd"]
    assert commitment["tokens"] == own["tokens"]
    assert many_rows == few_rows
