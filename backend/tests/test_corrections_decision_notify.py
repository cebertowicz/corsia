"""A decision answer commits on a DB thread; its arrival reaches the corrections worker on the app loop.

The worker's queue, event and task are bound to the application loop, so its
``notify`` hands a commit effect from a DB pool thread to ``infra.app_loop``,
and with no application loop changes nothing: startup's owed scan reads the answer,
recording its fact without a note.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from plans import decisions
from tests.test_correction_notes import _version
from tests.test_plan_decision_answer import _human, _raised_decision
from tests.test_plan_decisions import _DbProxy, _mid, db  # noqa: F401 - shared fixture

OTHER = {"option_id": "other", "other_text": "Neither."}


def _answer(db, decision):
    return decisions.answer(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=_human(), provenance="p", db=db, **OTHER,
    )


def _isolated_worker(monkeypatch):
    from jobs import corrections_realtime as realtime

    worker = realtime.CorrectionsWorker()
    monkeypatch.setattr(realtime, "WORKER", worker)
    monkeypatch.setattr(worker, "_start_task", lambda coroutine: coroutine.close())
    return worker


@pytest.mark.asyncio
async def test_answer_through_run_in_db_notifies_the_worker_on_its_loop(db, monkeypatch):
    import infra.app_loop as app_loop
    from infra.db_async import run_in_db

    worker = _isolated_worker(monkeypatch)
    notified_on = []
    real_notify = worker.notify
    monkeypatch.setattr(
        worker, "notify", lambda *args: (notified_on.append(threading.get_ident()), real_notify(*args)),
    )
    app_loop.capture(asyncio.get_running_loop())  # what ``lifespan`` captures
    _plan, _source, decision = _raised_decision(db)

    try:
        await run_in_db(lambda: _answer(db, decision))
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        app_loop.clear()

    assert worker.queue.get_nowait()[:2] == ("plan_decision_answer", decision["id"])
    # Called on the DB pool's thread, it hopped once and enqueued on the loop's.
    assert len(notified_on) == 2 and notified_on[0] != notified_on[1] == threading.get_ident()


def test_answer_with_no_app_loop_changes_no_worker_state_and_startups_scan_reads_it(db, monkeypatch):
    from jobs.corrections_scanning import pending_source_candidate_keys

    worker = _isolated_worker(monkeypatch)  # the suite hides the app loop outside integration tests
    _plan, _source, decision = _raised_decision(db)
    before = worker.snapshot()

    _answer(db, decision)
    db.commit()

    # Off every loop, nothing may touch the worker's state; startup owes the scan that reads the answer.
    assert worker.snapshot() == before and worker.scan == "idle" and worker.queue.empty()
    monkeypatch.setattr("infra.database.get_db", lambda: _DbProxy(db))
    assert [(key.source_kind, key.source_id) for key in pending_source_candidate_keys(_version(), 10)] == [
        ("plan_decision_answer", decision["id"]),
    ]
