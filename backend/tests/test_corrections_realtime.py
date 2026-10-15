"""The corrections worker's state machine: one test per transition that can fail."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def _candidate(message_id: str = "owner-message"):
    from core.corrections_recording import CorrectionCandidate

    return CorrectionCandidate(
        message_id=message_id, thread_id="thread", project_id="project",
        content="That result is wrong.", created_at="2026-10-14T00:00:00Z",
        context=(("Agent", "Earlier answer."),),
    )


def _version():
    from core.corrections_recording import ClassifierVersion

    return ClassifierVersion(
        "corrections-classifier", "v1", "correction", 0.595681, "modernbert-owner-context-v2-2048",
    )


def _manifest():
    return SimpleNamespace(version="v1", classifier_version=lambda head="correction": _version())


def _result():
    return SimpleNamespace(label="not_correction", truncated=False, original_tokens=4, version=_version())


class ScriptedChild:
    """A model child whose loads and calls follow a script: ``ok`` or an exception per step."""

    created: list["ScriptedChild"] = []
    loads: list[object] = []
    calls: list[object] = []

    def __init__(self, manifest):
        self.loaded = False
        self.running = False
        self.closed = False
        self.killed = False
        ScriptedChild.created.append(self)

    def ensure_loaded(self):
        step = ScriptedChild.loads.pop(0) if ScriptedChild.loads else "ok"
        if step != "ok":
            raise step
        self.loaded = self.running = True

    def classify(self, context, owner):
        step = ScriptedChild.calls.pop(0) if ScriptedChild.calls else "ok"
        if step != "ok":
            self.running = False
            raise step
        return _result()

    def close(self):
        self.closed = True
        self.running = False

    def kill(self):
        self.killed = True


@pytest.fixture(autouse=True)
def worker(monkeypatch):
    from jobs import corrections_realtime

    fresh = corrections_realtime.CorrectionsWorker()
    monkeypatch.setattr(corrections_realtime, "WORKER", fresh)
    monkeypatch.setattr(corrections_realtime, "ClassifierChild", ScriptedChild)
    monkeypatch.setattr(corrections_realtime, "_manifest", _manifest)
    ScriptedChild.created, ScriptedChild.loads, ScriptedChild.calls = [], [], []
    return fresh


def _absent(error="classifier asset is absent"):
    from core.corrections_classifier import AssetError

    def raise_error():
        raise AssetError(error)

    return raise_error


# ---- classifier state --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absent_asset_records_nothing_and_says_absent(worker, monkeypatch, tmp_path):
    from jobs import corrections_realtime

    monkeypatch.setattr(corrections_realtime, "_manifest", _absent())
    monkeypatch.setattr(corrections_realtime, "asset_root", lambda: tmp_path / "missing")
    recorded = AsyncMock()
    monkeypatch.setattr(corrections_realtime, "_record", recorded)

    assert await worker._classify(_candidate()) is False

    assert recorded.await_count == 0
    assert worker.snapshot()["state"] == "absent"
    assert worker.snapshot()["retryable"] is True


@pytest.mark.asyncio
async def test_present_but_unpinned_asset_is_failed_and_not_retryable(worker, monkeypatch, tmp_path):
    from jobs import corrections_realtime

    (tmp_path / "manifest.json").write_text("invalid", encoding="utf-8")
    monkeypatch.setattr(corrections_realtime, "asset_root", lambda: tmp_path)
    monkeypatch.setattr(corrections_realtime, "_manifest", _absent("manifest sha256 does not match the source pin"))

    assert await worker._classify(_candidate()) is False

    state = worker.snapshot()
    assert (state["state"], state["retryable"]) == ("failed", False)
    assert "pin" in state["reason"]


@pytest.mark.asyncio
async def test_verified_manifest_alone_never_reports_ready(worker, monkeypatch):
    from jobs import corrections_realtime

    seen = []
    ScriptedChild.loads = ["ok"]

    async def record(candidate, result):
        seen.append(worker.state)
        return True

    monkeypatch.setattr(corrections_realtime, "_record", record)

    assert await worker._classify(_candidate()) is True

    # loading while the child starts and the call runs; ready only after the call succeeded.
    assert seen == ["loading"]
    assert worker.state == "ready" and worker.reason is None


@pytest.mark.asyncio
async def test_two_load_failures_leave_the_message_pending_and_say_unavailable(worker, monkeypatch):
    from core.corrections_child import ClassifierUnavailable
    from jobs import corrections_realtime

    ScriptedChild.loads = [ClassifierUnavailable("load timeout"), ClassifierUnavailable("load error:AssetError")]
    recorded = AsyncMock()
    monkeypatch.setattr(corrections_realtime, "_record", recorded)
    monkeypatch.setattr(corrections_realtime, "_record_unreadable", recorded)

    assert await worker._classify(_candidate()) is False

    assert recorded.await_count == 0
    assert (worker.state, worker.reason) == ("unavailable", "load error:AssetError")
    assert all(child.closed for child in ScriptedChild.created) and worker.child is None


@pytest.mark.asyncio
async def test_ready_classifier_whose_restart_cannot_load_becomes_unavailable(worker, monkeypatch):
    from core.corrections_child import ClassifierUnavailable, MessageFailed
    from jobs import corrections_realtime

    monkeypatch.setattr(corrections_realtime, "_record", AsyncMock(return_value=True))
    assert await worker._classify(_candidate()) is True
    assert worker.state == "ready"

    # The live child fails on this input, then neither restart loads.
    ScriptedChild.calls = [MessageFailed("rss")]
    ScriptedChild.loads = ["ok", ClassifierUnavailable("load rss"), ClassifierUnavailable("load rss")]

    assert await worker._classify(_candidate("second")) is False
    assert (worker.state, worker.reason) == ("unavailable", "load rss")


@pytest.mark.asyncio
async def test_input_that_fails_twice_on_a_healthy_child_is_recorded_unreadable(worker, monkeypatch):
    from core.corrections_child import MessageFailed
    from jobs import corrections_realtime

    monkeypatch.setattr(corrections_realtime, "_record", AsyncMock(return_value=True))
    assert await worker._classify(_candidate()) is True
    unreadable = AsyncMock(return_value=True)
    monkeypatch.setattr(corrections_realtime, "_record_unreadable", unreadable)
    ScriptedChild.calls = [MessageFailed("timeout"), MessageFailed("rss")]

    assert await worker._classify(_candidate("huge")) is True

    assert unreadable.await_args.args[0].message_id == "huge"
    assert unreadable.await_args.args[2] == "rss"
    # An unreadable input is an outcome, not an outage: the classifier stays ready.
    assert worker.state == "ready"


@pytest.mark.asyncio
async def test_unreadable_fact_is_written_with_its_reason_and_not_counted(isolated_db, worker, monkeypatch):
    from core.corrections_child import MessageFailed
    from jobs import corrections_realtime
    from jobs.corrections_activation import weekly_correction_figure

    ids = _seed_owner_messages(1)
    candidate = await asyncio.to_thread(corrections_realtime._candidate, ids[0])
    ScriptedChild.calls = [MessageFailed("child_error"), MessageFailed("child_error")]

    assert await worker._classify(candidate) is True

    from infra.database import get_db

    db = get_db()
    try:
        assert corrections_realtime.state_snapshot(db)["unreadable_messages"] == 1
    finally:
        db.close()
    assert weekly_correction_figure(_version(), since="2000-01-01", weeks=1) == 0


@pytest.mark.asyncio
async def test_classifier_work_is_moved_off_the_event_loop(worker, monkeypatch):
    from jobs import corrections_realtime

    real_to_thread = asyncio.to_thread
    calls = []

    async def observed_to_thread(function, *args, **kwargs):
        calls.append(getattr(function, "__name__", function))
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(corrections_realtime.asyncio, "to_thread", observed_to_thread)
    monkeypatch.setattr(corrections_realtime, "_record", AsyncMock(return_value=True))

    await worker._classify(_candidate())

    assert calls == ["_manifest", "ensure_loaded", "classify"]


@pytest.mark.asyncio
async def test_inference_runs_outside_a_database_write_transaction(isolated_db, worker, monkeypatch):
    from jobs import corrections_realtime

    class Child(ScriptedChild):
        def classify(self, context, owner):
            from infra.database import get_db

            db = get_db()
            try:
                assert db.in_transaction is False
            finally:
                db.close()
            return _result()

    monkeypatch.setattr(corrections_realtime, "ClassifierChild", Child)
    monkeypatch.setattr(corrections_realtime, "_record", AsyncMock(return_value=True))

    assert await worker._classify(_candidate()) is True


# ---- scan status -------------------------------------------------------------------------


def test_full_arrival_queue_keeps_send_admission_and_owes_the_scan(worker):
    worker.queue = asyncio.Queue(maxsize=1)
    worker.queue.put_nowait(("already-queued", 0.0))

    worker.notify("dropped")

    state = worker.snapshot()
    assert state["dropped_arrivals"] == 1
    assert state["scan"] == "owed"
    assert worker.queue.qsize() == 1


@pytest.mark.asyncio
async def test_becoming_ready_from_another_state_owes_a_catch_up_scan(worker, monkeypatch):
    from jobs import corrections_realtime

    monkeypatch.setattr(corrections_realtime, "_record", AsyncMock(return_value=True))

    assert await worker._classify(_candidate()) is True
    assert worker.scan == "owed"


@pytest.mark.asyncio
async def test_outage_holds_the_scan_before_activation_and_an_arrival_releases_it(isolated_db, worker, monkeypatch):
    from core.corrections_child import ClassifierUnavailable
    from jobs import corrections_realtime

    ids = _seed_owner_messages(3)
    activated = AsyncMock(return_value="done")
    monkeypatch.setattr(corrections_realtime, "_activate_after_backfill", activated)
    ScriptedChild.loads = [ClassifierUnavailable("load timeout")] * 2

    worker.scan = "owed"
    await worker.run()

    assert activated.await_count == 0
    assert _recorded_message_ids() == set()
    assert (worker.scan, worker.held_reason) == ("owed", "classifier unavailable: load timeout")

    # A later arrival classifies: the hold clears and the owed scan finishes the backlog.
    worker.queue.put_nowait((ids[0], 0.0))
    await worker.run()

    assert _recorded_message_ids() == set(ids)
    assert activated.await_count == 1
    assert (worker.scan, worker.held_reason) == ("idle", None)


@pytest.mark.asyncio
async def test_a_failing_scan_pass_keeps_the_scan_owed_and_the_worker_alive(worker, monkeypatch):
    from jobs import corrections_realtime, corrections_recording

    async def locked(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(corrections_recording, "_next_pending_batch", locked)
    processed = []
    monkeypatch.setattr(corrections_realtime, "_candidate", lambda message_id: processed.append(message_id))

    worker.scan = "owed"
    worker.queue.put_nowait(("arrival", 0.0))
    await worker.run()

    assert processed == ["arrival"]
    assert (worker.scan, worker.held_reason) == ("owed", "scan error: RuntimeError")


@pytest.mark.asyncio
async def test_a_failing_arrival_owes_the_scan_and_the_next_arrival_still_runs(worker, monkeypatch):
    from jobs import corrections_realtime

    def candidate(message_id):
        if message_id == "first":
            raise RuntimeError("database is locked")
        return None

    monkeypatch.setattr(corrections_realtime, "_candidate", candidate)
    monkeypatch.setattr(worker, "_scan_pass", AsyncMock(side_effect=lambda: worker._finish_scan()))
    worker.queue.put_nowait(("first", 0.0))
    worker.queue.put_nowait(("second", 0.0))

    await worker.run()

    assert worker.queue.qsize() == 0
    assert worker._scan_pass.await_count == 1


@pytest.mark.asyncio
async def test_worker_task_that_dies_keeps_the_scan_owed_and_notify_restarts_it(worker, monkeypatch):
    from jobs import corrections_realtime

    async def crash():
        raise RuntimeError("unexpected")

    worker._start_task(crash())
    with pytest.raises(RuntimeError):
        await worker.task
    await asyncio.sleep(0)  # done callbacks run on the next loop step

    assert (worker.scan, worker.held_reason) == ("owed", "worker error: RuntimeError")

    seen = []
    monkeypatch.setattr(corrections_realtime, "_candidate", lambda message_id: seen.append(message_id))
    corrections_realtime.notify("after-crash")
    await worker.task

    assert seen == ["after-crash"]


@pytest.mark.asyncio
async def test_bounded_scan_passes_measurement_cutoff_to_selector(worker, monkeypatch):
    from jobs import corrections_realtime, corrections_recording

    selected = []

    async def next_batch(*args, **kwargs):
        selected.append(kwargs["since"])
        return SimpleNamespace(selected_keys=[], candidates=[])

    monkeypatch.setattr(corrections_recording, "_next_pending_batch", next_batch)
    monkeypatch.setattr(corrections_realtime, "_activate_after_backfill", AsyncMock(return_value="done"))

    worker.scan = "owed"
    await worker.run()

    assert len(selected) == 1 and selected[0]
    assert worker.scan == "idle"


@pytest.mark.asyncio
async def test_completed_capped_scan_activates_and_notifies_once(worker, monkeypatch):
    from core import corrections_recording as core_recording
    from jobs import corrections_activation, corrections_recording

    complete = Mock(return_value=True)
    notified = AsyncMock()

    async def next_batch(*args, **kwargs):
        return SimpleNamespace(selected_keys=[], candidates=[])

    monkeypatch.setattr(corrections_recording, "_next_pending_batch", next_batch)
    monkeypatch.setattr(corrections_recording, "_owner_user_ids", lambda: ["owner"])
    monkeypatch.setattr(core_recording, "active_corrections_version", lambda: None)
    monkeypatch.setattr(corrections_activation, "weekly_correction_figure", lambda version, **_kwargs: 3.0)
    monkeypatch.setattr(corrections_activation, "complete_backfill", complete)
    monkeypatch.setattr(corrections_activation, "tell_conductor_about_activation", notified)

    worker.scan = "owed"
    await worker.run()

    assert complete.call_count == 1
    assert complete.call_args.kwargs["weeks"] > 0
    notified.assert_awaited_once()


def test_startup_cleanup_discards_interrupted_asset_staging(monkeypatch, tmp_path):
    from jobs import corrections_realtime

    root = tmp_path / "corrections-classifier"
    staging = tmp_path / "corrections-classifier.staging"
    staging.mkdir()
    (staging / "partial.onnx").write_bytes(b"partial")
    monkeypatch.setattr(corrections_realtime, "asset_root", lambda: root)

    corrections_realtime._cleanup_staging()

    assert not staging.exists()


@pytest.mark.asyncio
async def test_backfill_yields_after_one_in_flight_call_when_an_arrival_is_queued(worker, monkeypatch):
    from jobs import corrections_recording

    first, second = _candidate(), _candidate("older-message")
    batch = SimpleNamespace(
        selected_keys=[
            SimpleNamespace(created_at=first.created_at, message_id=first.message_id),
            SimpleNamespace(created_at=second.created_at, message_id=second.message_id),
        ],
        candidates=[first, second],
    )
    calls = []

    async def next_batch(*args, **kwargs):
        return batch

    async def classify(candidate):
        calls.append(candidate.message_id)
        worker.queue.put_nowait(("fresh-arrival", 0.0))
        return True

    monkeypatch.setattr(corrections_recording, "_next_pending_batch", next_batch)
    monkeypatch.setattr(worker, "_classify", classify)

    worker.scan = "owed"
    await worker._scan_pass()

    assert calls == ["owner-message"]
    assert worker.scan == "owed" and worker.held_reason is None


@pytest.mark.asyncio
async def test_backfill_waits_one_call_duration_before_the_next_low_priority_call(worker, monkeypatch):
    from jobs import corrections_realtime, corrections_recording

    candidate = _candidate()
    batches = [
        SimpleNamespace(
            selected_keys=[SimpleNamespace(created_at=candidate.created_at, message_id=candidate.message_id)],
            candidates=[candidate],
        ),
        SimpleNamespace(selected_keys=[], candidates=[]),
    ]
    slept = AsyncMock()

    async def next_batch(*args, **kwargs):
        return batches.pop(0)

    monkeypatch.setattr(corrections_recording, "_next_pending_batch", next_batch)
    monkeypatch.setattr(worker, "_classify", AsyncMock(return_value=True))
    monkeypatch.setattr(corrections_realtime, "_activate_after_backfill", AsyncMock(return_value="done"))
    monkeypatch.setattr(corrections_realtime.asyncio, "sleep", slept)
    ticks = iter((10.0, 13.5))
    monkeypatch.setattr(corrections_realtime, "_clock", lambda: next(ticks))

    await worker._scan_pass()

    slept.assert_awaited_once_with(3.5)
    assert worker.scan == "idle"


@pytest.mark.asyncio
async def test_one_worker_processes_arrival_queued_during_startup_scan(worker, monkeypatch):
    processed = []

    async def scan():
        worker.queue.put_nowait(("during-startup", 0.0))
        worker._finish_scan()

    async def arrival(message_id, enqueued_at):
        processed.append(message_id)
        worker.queue.task_done()

    monkeypatch.setattr(worker, "_scan_pass", scan)
    monkeypatch.setattr(worker, "_arrival", arrival)
    worker.scan = "owed"

    await worker.run()

    assert processed == ["during-startup"]


@pytest.mark.asyncio
async def test_restart_retries_only_an_unaccepted_activation_notice(monkeypatch):
    from core import corrections_recording as core_recording
    from jobs import corrections_activation, corrections_realtime

    version = _version()
    sent = AsyncMock()
    monkeypatch.setattr(core_recording, "active_corrections_version", lambda: version)
    monkeypatch.setattr(corrections_activation, "tell_conductor_about_activation", sent)

    await corrections_realtime._retry_pending_activation_notice()

    sent.assert_awaited_once_with(version)
    for absent_or_corrupt in (None, core_recording.CorruptActivation("KeyError")):
        monkeypatch.setattr(core_recording, "active_corrections_version", lambda value=absent_or_corrupt: value)
        await corrections_realtime._retry_pending_activation_notice()
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_the_worker_kills_the_child_and_owes_later_arrivals(worker, monkeypatch):
    from jobs import corrections_realtime

    started = asyncio.Event()

    async def blocked_worker():
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(corrections_realtime, "_startup_worker", blocked_worker)
    monkeypatch.setattr(corrections_realtime, "_cleanup_staging", lambda: None)
    child = ScriptedChild(None)
    worker.child = child
    corrections_realtime.startup_scan()
    task = worker.task
    await started.wait()

    await corrections_realtime.shutdown()

    assert task is not None and task.cancelled()
    assert child.killed and child.closed
    assert worker.child is None

    corrections_realtime.notify("arrived-during-shutdown")

    assert worker.task is None
    assert worker.queue.qsize() == 0
    assert corrections_realtime.state_snapshot()["scan_owed"] is True


# ---- real model processes ----------------------------------------------------------------


def _stuck_in_load(connection, manifest_path):
    """A model child that never finishes loading (module level: spawn pickles it by name)."""
    import time
    from pathlib import Path

    Path(manifest_path).with_name("in-load").touch()
    time.sleep(120)


def _stuck_in_call(connection, manifest_path):
    """A model child that loads, then never answers its first input."""
    import time
    from pathlib import Path

    connection.send({"ready": True})
    connection.recv()
    Path(manifest_path).with_name("in-call").touch()
    time.sleep(120)


@pytest.mark.parametrize(("target", "marker"), [(_stuck_in_load, "in-load"), (_stuck_in_call, "in-call")])
async def test_shutdown_during_a_slow_load_or_call_leaves_no_model_process(
    worker, monkeypatch, tmp_path, target, marker,
):
    import os

    from core.corrections_child import ClassifierChild
    from jobs import corrections_realtime

    children = []

    def real_child(manifest):
        children.append(ClassifierChild(manifest, target=target))
        return children[-1]

    monkeypatch.setattr(corrections_realtime, "ClassifierChild", real_child)
    monkeypatch.setattr(corrections_realtime, "_manifest", lambda: SimpleNamespace(
        path=tmp_path / "manifest.json", version="v1", classifier_version=lambda head="correction": _version(),
    ))
    worker.task = asyncio.create_task(worker._classify(_candidate()))
    for _ in range(600):
        if (tmp_path / marker).exists():
            break
        await asyncio.sleep(0.05)
    assert (tmp_path / marker).exists()
    process = children[0]._process
    assert process is not None and process.is_alive()

    await asyncio.wait_for(corrections_realtime.shutdown(), timeout=20)

    assert process.exitcode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)
    assert worker.child is None and children[0]._process is None
    assert len(children) == 1


# ---- database-backed scans ---------------------------------------------------------------


def _seed_owner_messages(count: int) -> list[str]:
    """Newest-last owner messages inside the measurement window; returns their ids."""
    from datetime import datetime, timedelta, timezone

    from infra.database import get_db

    now = datetime.now(timezone.utc)
    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','project','t','t')")
        db.execute(
            "INSERT INTO threads (id,user_id,title,project_id,created_at,updated_at) "
            "VALUES ('thread','owner','thread','project','t','t')"
        )
        ids = []
        for index in range(count):
            message_id = f"owner-{index:03d}"
            created_at = (now - timedelta(minutes=count - index)).isoformat()
            db.execute(
                "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (message_id, "thread", "user", f"synthetic owner message {index}", "{}", created_at),
            )
            ids.append(message_id)
        db.commit()
        return ids
    finally:
        db.close()


def _recorded_message_ids() -> set[str]:
    from infra.database import get_db

    db = get_db()
    try:
        return {
            str(row[0]) for row in db.execute(
                "SELECT json_extract(attributes,'$.message_id') FROM outcome_events"
            ).fetchall()
        }
    finally:
        db.close()


@pytest.mark.asyncio
async def test_recorded_newest_messages_do_not_hide_older_pending_ones(isolated_db, worker, monkeypatch):
    from jobs import corrections_realtime

    ids = _seed_owner_messages(30)
    seen_at_activation = []

    async def activate(manifest):
        seen_at_activation.append(_recorded_message_ids())
        return "done"

    monkeypatch.setattr(corrections_realtime, "_activate_after_backfill", activate)

    # The 22 newest are already recorded: more than one 20-message page.
    for message_id in ids[-22:]:
        candidate = await asyncio.to_thread(corrections_realtime._candidate, message_id)
        assert await corrections_realtime._record(candidate, _result())
    assert _recorded_message_ids() == set(ids[-22:])

    await worker._scan_pass()

    assert seen_at_activation == [set(ids)]
    assert worker.scan == "idle"
