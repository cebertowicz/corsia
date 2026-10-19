"""A decision answer commits on a DB thread; its arrival reaches the corrections worker on the app loop.

The worker's queue, event and task are loop-bound, so the commit effect hops to
the application loop, and with no loop the scan is owed and recovers the answer.
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
async def test_answer_through_run_in_db_notifies_the_worker_on_the_app_loop(db, monkeypatch):
    from infra import app_loop
    from infra.db_async import run_in_db

    worker = _isolated_worker(monkeypatch)
    notified_on = []
    real_notify = worker.notify
    monkeypatch.setattr(
        worker, "notify", lambda *args: (notified_on.append(threading.get_ident()), real_notify(*args)),
    )
    monkeypatch.setattr(app_loop, "_LOOP", asyncio.get_running_loop())
    _plan, _source, decision = _raised_decision(db)

    await run_in_db(lambda: _answer(db, decision))
    for _ in range(5):
        await asyncio.sleep(0)

    assert worker.queue.get_nowait()[:2] == ("plan_decision_answer", decision["id"])
    assert notified_on == [threading.get_ident()]  # the loop's thread, not the DB pool's


def test_answer_with_no_app_loop_owes_the_scan_which_recovers_the_answer(db, monkeypatch):
    from jobs.corrections_scanning import pending_source_candidate_keys

    worker = _isolated_worker(monkeypatch)  # the suite hides the app loop outside integration tests
    _plan, _source, decision = _raised_decision(db)

    _answer(db, decision)
    db.commit()

    assert worker.scan == "owed" and worker.queue.empty()
    monkeypatch.setattr("infra.database.get_db", lambda: _DbProxy(db))
    assert [(key.source_kind, key.source_id) for key in pending_source_candidate_keys(_version(), 10)] == [
        ("plan_decision_answer", decision["id"]),
    ]
