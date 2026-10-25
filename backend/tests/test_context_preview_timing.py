from __future__ import annotations

import json

import pytest
from loguru import logger

from infra.context_preview_timing import (
    ContextPreviewSummary,
    ContextPreviewTiming,
    record_projection_lock_wait,
)


@pytest.fixture(autouse=True)
def _isolated_summary(monkeypatch):
    # The class-level summary is process-wide; once its interval passes in a long
    # test process it logs a line here. Each test gets its own that never fires.
    monkeypatch.setattr(
        ContextPreviewTiming, "_summary", ContextPreviewSummary(interval_seconds=float("inf")),
    )


def test_context_preview_timing_emits_one_content_free_structured_line():
    records = []
    sink = logger.add(lambda message: records.append(message.record))
    try:
        timing = ContextPreviewTiming("thread-1", threshold_ms=-1)
        assert timing.measure("preflight", lambda: "ok") == "ok"
        timing.emit_if_slow({"history_messages": 164, "total_used": 185_000})
    finally:
        logger.remove(sink)

    assert len(records) == 1
    name, encoded = records[0]["message"].split(" ", 1)
    assert name == "context_preview_timing"
    payload = json.loads(encoded)
    assert payload["event"] == "context_preview_timing"
    assert payload["thread_id"] == "thread-1"
    assert payload["total_ms"] >= payload["stages"]["preflight"]["wall_ms"]
    assert payload["message_count"] == 164
    assert payload["token_estimate"] == 185_000
    assert payload["executor_queue_delay_ms"] == 0.0
    assert payload["projection_lock_wait_ms"] == 0.0
    assert set(payload["stages"]["preflight"]) == {"wall_ms", "cpu_ms"}


def test_context_preview_timing_keeps_nested_substages_content_free():
    timing = ContextPreviewTiming("thread-1")
    timing.measure("preflight.checkpoint_tail.current_installed", lambda: None)
    timing.measure("preflight.checkpoint_tail.tail_render", lambda: None)
    timing.measure("preflight.checkpoint_tail.interpreter_yield", lambda: None)

    assert timing.stages.keys() == {
        "preflight.checkpoint_tail.current_installed",
        "preflight.checkpoint_tail.tail_render",
        "preflight.checkpoint_tail.interpreter_yield",
    }
    assert all(set(stage) == {"wall_ms", "cpu_ms"} for stage in timing.stages.values())


def test_context_preview_summary_is_interval_bounded_and_resets_counts():
    summary = ContextPreviewSummary(interval_seconds=300, last_emitted_at=0)

    assert summary.record(99, now=299) is None
    payload = summary.record(1_001, now=300)

    assert payload == {
        "event": "context_preview_summary",
        "interval_seconds": 300,
        "count": 2,
        "total_ms_buckets": {
            "le_100ms": 1,
            "le_500ms": 0,
            "le_1s": 0,
            "le_5s": 1,
            "le_15s": 0,
            "le_30s": 0,
            "le_60s": 0,
            "gt_60s": 0,
        },
    }
    assert summary.record(70_000, now=599) is None
    assert summary.record(70_000, now=600)["count"] == 2


def test_context_preview_summary_reports_actual_elapsed_interval():
    summary = ContextPreviewSummary(interval_seconds=300, last_emitted_at=0)

    assert summary.record(99, now=0) is None
    assert summary.record(99, now=1_000)["interval_seconds"] == 1_000


def test_context_preview_summary_logs_without_fast_preview_rows(monkeypatch):
    records = []
    sink = logger.add(lambda message: records.append(message.record))
    monkeypatch.setattr(
        ContextPreviewTiming,
        "_summary",
        ContextPreviewSummary(interval_seconds=0, last_emitted_at=0),
    )
    try:
        ContextPreviewTiming("thread-1", threshold_ms=float("inf")).emit_if_slow({})
    finally:
        logger.remove(sink)

    name, encoded = records[0]["message"].split(" ", 1)
    payload = json.loads(encoded)
    assert name == "context_preview_summary"
    assert payload["count"] == 1
    assert sum(payload["total_ms_buckets"].values()) == 1


def test_context_preview_timing_records_executor_queue_and_projection_lock_wait():
    records = []
    sink = logger.add(lambda message: records.append(message.record))
    try:
        timing = ContextPreviewTiming("thread-1", threshold_ms=-1)
        wrapped = timing.wrap(
            "preview_job",
            lambda: record_projection_lock_wait(0.012),
            submitted_at=timing.started_at - 0.025,
        )
        wrapped()
        timing.emit_if_slow({})
    finally:
        logger.remove(sink)

    payload = json.loads(records[0]["message"].split(" ", 1)[1])
    assert payload["executor_queue_delay_ms"] >= 25.0
    assert payload["projection_lock_wait_ms"] == 12.0


def test_context_preview_timing_stays_silent_below_injected_threshold():
    records = []
    sink = logger.add(lambda message: records.append(message.record))
    try:
        timing = ContextPreviewTiming("thread-1", threshold_ms=float("inf"))
        timing.emit_if_slow({"history_messages": 164, "total_used": 185_000})
    finally:
        logger.remove(sink)

    assert records == []
