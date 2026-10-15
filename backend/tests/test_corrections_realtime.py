from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def _candidate():
    from core.corrections_recording import CorrectionCandidate

    return CorrectionCandidate(
        message_id="owner-message", thread_id="thread", project_id="project",
        content="That result is wrong.", created_at="2026-10-14T00:00:00Z",
        preceding_agent="Earlier answer.", context=(("assistant", "Earlier answer."),),
    )


def _manifest():
    return SimpleNamespace(
        version="v1",
        checker_version=lambda: SimpleNamespace(
            asset_id="corrections-classifier", version="v1", head="correction",
            threshold=0.595681, input_policy_id="modernbert-owner-context-v2-2048",
        ),
    )


@pytest.fixture(autouse=True)
def _reset_realtime(monkeypatch):
    from jobs import corrections_realtime

    monkeypatch.setattr(corrections_realtime, "_QUEUE", asyncio.Queue(maxsize=256))
    monkeypatch.setattr(corrections_realtime, "_TASK", None)
    monkeypatch.setattr(corrections_realtime, "_CHILD", None)
    monkeypatch.setattr(corrections_realtime, "_SCAN_OWED", False)
    monkeypatch.setattr(corrections_realtime, "_STOPPING", False)
    monkeypatch.setattr(
        corrections_realtime, "_STATE",
        {"state": "absent", "reason": "asset not verified", "retryable": True,
         "dropped_arrivals": 0, "queue_delay_ms": 0.0, "service_time_ms": 0.0},
    )


def test_full_arrival_queue_keeps_send_admission_and_records_the_drop(monkeypatch):
    from jobs import corrections_realtime

    monkeypatch.setattr(corrections_realtime, "_QUEUE", asyncio.Queue(maxsize=1))
    corrections_realtime._QUEUE.put_nowait(("already-queued", 0.0))

    corrections_realtime.notify("dropped")

    state = corrections_realtime.state_snapshot()
    assert state["dropped_arrivals"] == 1
    assert state["scan_owed"] is True
    assert corrections_realtime._QUEUE.qsize() == 1


@pytest.mark.asyncio
async def test_absent_asset_leaves_message_pending_without_a_refusal_fact(monkeypatch):
    from core.corrections_classifier import AssetError
    from jobs import corrections_realtime

    monkeypatch.setattr(corrections_realtime, "_manifest", lambda: (_ for _ in ()).throw(AssetError("classifier asset is absent")))
    recorded = AsyncMock()
    monkeypatch.setattr(corrections_realtime, "_record", recorded)
    monkeypatch.setattr(corrections_realtime, "_record_unclassifiable", recorded)

    await corrections_realtime._classify(_candidate())

    assert recorded.await_count == 0
    assert corrections_realtime.state_snapshot()["state"] == "absent"


@pytest.mark.asyncio
async def test_child_crash_is_tried_twice_then_records_one_terminal_fact(monkeypatch):
    from core.corrections_classifier import ClassifierUnavailable
    from jobs import corrections_realtime

    class CrashingChild:
        def __init__(self, manifest):
            self.calls = 0

        def classify(self, context, owner):
            self.calls += 1
            raise ClassifierUnavailable("child exited")

    terminal = AsyncMock(return_value=True)
    monkeypatch.setattr(corrections_realtime, "_manifest", _manifest)
    monkeypatch.setattr(corrections_realtime, "ClassifierChild", CrashingChild)
    monkeypatch.setattr(corrections_realtime, "_record_unclassifiable", terminal)

    await corrections_realtime._classify(_candidate())

    assert corrections_realtime._CHILD is None
    assert terminal.await_count == 1
    assert terminal.await_args.args[2] == "child exited"


@pytest.mark.asyncio
async def test_classifier_work_is_moved_off_the_event_loop(monkeypatch):
    from jobs import corrections_realtime

    class Child:
        def __init__(self, manifest):
            pass

        def classify(self, context, owner):
            return SimpleNamespace(
                label="not_correction", truncated=False, original_tokens=4,
                version=_manifest().checker_version(),
            )

    real_to_thread = asyncio.to_thread
    calls = []

    async def observed_to_thread(function, *args, **kwargs):
        calls.append(function)
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(corrections_realtime, "_manifest", _manifest)
    monkeypatch.setattr(corrections_realtime, "ClassifierChild", Child)
    monkeypatch.setattr(corrections_realtime.asyncio, "to_thread", observed_to_thread)
    monkeypatch.setattr(corrections_realtime, "_record", AsyncMock(return_value=True))

    await corrections_realtime._classify(_candidate())

    assert len(calls) == 2
    assert corrections_realtime._SCAN_OWED is True


@pytest.mark.asyncio
async def test_bounded_scan_passes_measurement_cutoff_to_selector(monkeypatch):
    from jobs import corrections_realtime, corrections_recording

    selected = []

    async def next_batch(*args, **kwargs):
        selected.append(kwargs["since"])
        return SimpleNamespace(selected_keys=[], candidates=[])

    monkeypatch.setattr(corrections_realtime, "_manifest", _manifest)
    monkeypatch.setattr(corrections_recording, "_next_pending_batch", next_batch)
    monkeypatch.setattr(corrections_recording, "_owner_user_ids", lambda: [])

    await corrections_realtime._catch_up_scan()

    assert len(selected) == 1
    assert selected[0]


@pytest.mark.asyncio
async def test_completed_capped_scan_activates_and_notifies_once(monkeypatch):
    from core import corrections_recording as core_recording
    from jobs import corrections_activation, corrections_realtime, corrections_recording

    complete = Mock(return_value=True)
    notified = AsyncMock()

    async def next_batch(*args, **kwargs):
        return SimpleNamespace(selected_keys=[], candidates=[])

    monkeypatch.setattr(corrections_realtime, "_manifest", _manifest)
    monkeypatch.setattr(corrections_recording, "_next_pending_batch", next_batch)
    monkeypatch.setattr(corrections_recording, "_owner_user_ids", lambda: ["owner"])
    monkeypatch.setattr(core_recording, "active_corrections_version", lambda: None)
    monkeypatch.setattr(
        corrections_activation, "weekly_correction_figure", lambda version, **_kwargs: 3.0,
    )
    monkeypatch.setattr(corrections_activation, "complete_backfill", complete)
    monkeypatch.setattr(corrections_activation, "tell_conductor_about_activation", notified)

    await corrections_realtime._catch_up_scan()

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
async def test_invalid_present_asset_surfaces_a_nonretryable_failed_state(monkeypatch, tmp_path):
    from core.corrections_classifier import AssetError
    from jobs import corrections_realtime

    root = tmp_path / "corrections-classifier"
    root.mkdir()
    (root / "manifest.json").write_text("invalid", encoding="utf-8")
    monkeypatch.setattr(corrections_realtime, "asset_root", lambda: root)
    monkeypatch.setattr(
        corrections_realtime, "_manifest",
        lambda: (_ for _ in ()).throw(AssetError("manifest sha256 does not match the source pin")),
    )

    await corrections_realtime._classify(_candidate())

    state = corrections_realtime.state_snapshot()
    assert state["state"] == "failed"
    assert state["retryable"] is False
    assert "pin" in state["reason"]


@pytest.mark.asyncio
async def test_backfill_yields_after_one_in_flight_call_when_an_arrival_is_queued(monkeypatch):
    from jobs import corrections_realtime, corrections_recording

    first = _candidate()
    second = _candidate()
    batch = SimpleNamespace(
        selected_keys=[
            SimpleNamespace(created_at=first.created_at, message_id=first.message_id),
            SimpleNamespace(created_at=second.created_at, message_id="older-message"),
        ],
        candidates=[first, second],
    )
    calls = []

    async def next_batch(*args, **kwargs):
        return batch

    async def classify(candidate):
        calls.append(candidate.message_id)
        corrections_realtime._QUEUE.put_nowait(("fresh-arrival", 0.0))

    monkeypatch.setattr(corrections_realtime, "_manifest", _manifest)
    monkeypatch.setattr(corrections_recording, "_next_pending_batch", next_batch)
    monkeypatch.setattr(corrections_realtime, "_classify", classify)

    await corrections_realtime._catch_up_scan()

    assert calls == ["owner-message"]


@pytest.mark.asyncio
async def test_inference_runs_outside_a_database_write_transaction(monkeypatch):
    from jobs import corrections_realtime

    class Child:
        def __init__(self, manifest):
            pass

        def classify(self, context, owner):
            from infra.database import get_db

            db = get_db()
            try:
                assert db.in_transaction is False
            finally:
                db.close()
            return SimpleNamespace(
                label="not_correction", truncated=False, original_tokens=4,
                version=_manifest().checker_version(),
            )

    monkeypatch.setattr(corrections_realtime, "_manifest", _manifest)
    monkeypatch.setattr(corrections_realtime, "ClassifierChild", Child)
    monkeypatch.setattr(corrections_realtime, "_record", AsyncMock(return_value=True))

    await corrections_realtime._classify(_candidate())


@pytest.mark.asyncio
async def test_one_worker_processes_arrival_queued_during_startup_scan(monkeypatch):
    from jobs import corrections_realtime

    processed = []

    async def scan():
        corrections_realtime._QUEUE.put_nowait(("during-startup", 0.0))

    async def process(message_id, enqueued_at):
        processed.append(message_id)
        corrections_realtime._QUEUE.task_done()

    monkeypatch.setattr(corrections_realtime, "_catch_up_scan", scan)
    monkeypatch.setattr(corrections_realtime, "_process_arrival", process)
    monkeypatch.setattr(corrections_realtime, "_SCAN_OWED", True)

    await corrections_realtime._worker()

    assert processed == ["during-startup"]


@pytest.mark.asyncio
async def test_restart_retries_only_an_unaccepted_activation_notice(monkeypatch):
    from core import corrections_recording as core_recording
    from jobs import corrections_activation, corrections_realtime

    version = _manifest().checker_version()
    sent = AsyncMock()
    monkeypatch.setattr(core_recording, "active_corrections_version", lambda: version)
    monkeypatch.setattr(corrections_activation, "tell_conductor_about_activation", sent)

    await corrections_realtime._retry_pending_activation_notice()

    sent.assert_awaited_once_with(version)
    monkeypatch.setattr(core_recording, "active_corrections_version", lambda: None)
    await corrections_realtime._retry_pending_activation_notice()
    assert sent.await_count == 1


@pytest.mark.asyncio
async def test_backfill_waits_one_call_duration_before_the_next_low_priority_call(monkeypatch):
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

    monkeypatch.setattr(corrections_realtime, "_manifest", _manifest)
    monkeypatch.setattr(corrections_recording, "_next_pending_batch", next_batch)
    monkeypatch.setattr(corrections_realtime, "_classify", AsyncMock())
    monkeypatch.setattr(corrections_realtime, "_activate_after_backfill", AsyncMock())
    monkeypatch.setattr(corrections_realtime.asyncio, "sleep", slept)
    ticks = iter((10.0, 13.5))
    monkeypatch.setattr(corrections_realtime, "_clock", lambda: next(ticks))

    await corrections_realtime._catch_up_scan()

    slept.assert_awaited_once_with(3.5)


@pytest.mark.asyncio
async def test_shutdown_joins_the_worker_and_closes_the_child(monkeypatch):
    from jobs import corrections_realtime

    started = asyncio.Event()

    async def blocked_worker():
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(corrections_realtime, "_startup_worker", blocked_worker)
    monkeypatch.setattr(corrections_realtime, "_cleanup_staging", lambda: None)
    child = Mock()
    monkeypatch.setattr(corrections_realtime, "_CHILD", child)
    corrections_realtime.startup_scan()
    task = corrections_realtime._TASK
    await started.wait()

    await corrections_realtime.shutdown()

    assert task is not None and task.cancelled()
    child.close.assert_called_once_with()
    assert corrections_realtime._CHILD is None

    corrections_realtime.notify("arrived-during-shutdown")

    assert corrections_realtime._TASK is None
    assert corrections_realtime._QUEUE.qsize() == 0
    assert corrections_realtime.state_snapshot()["scan_owed"] is True
