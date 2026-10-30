"""The bounded workflow verdict batch in the existing nightly scheduler."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.workflow_verdict_rules import Snapshot, WorkflowHistory
from core.workflow_verdicts import Run
from tests.test_workflow_verdict_read import NOW, db, live_workflow_owners, node  # noqa: F401 - shared fixtures


def _snapshot() -> Snapshot:
    return Snapshot("2026-10-20T00:00:00+00:00", (), subjects=frozenset({"workflow:test@v1"}))


def test_daily_verdict_batch_is_read_only_while_writes_are_disabled(monkeypatch) -> None:
    from jobs import workflow_verdicts

    db = MagicMock()
    evaluation = SimpleNamespace(verdict=SimpleNamespace(word="keep", reason="quality_better"))
    read = MagicMock(return_value=_snapshot())
    priced = MagicMock(return_value=_snapshot())
    write = MagicMock(return_value=1)
    monkeypatch.setattr(workflow_verdicts, "get_db", lambda: db)
    monkeypatch.setattr(workflow_verdicts, "read_open_snapshot", read)
    monkeypatch.setattr(workflow_verdicts, "price_snapshot", priced)
    monkeypatch.setattr(workflow_verdicts, "compute", lambda _snapshot: [evaluation])
    monkeypatch.setattr(workflow_verdicts, "evaluate_trials", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(workflow_verdicts.evaluator, "VERDICT_WRITES_ENABLED", False)
    monkeypatch.setattr(workflow_verdicts.evaluator, "write_verdicts", write)

    report = workflow_verdicts.run_daily()

    assert report.open_subjects == report.evaluated == report.reached == 1
    assert report.recorded == 0
    assert report.by_reason == {"quality_better": 1}
    read.assert_called_once()
    priced.assert_called_once_with(db, _snapshot())
    db.close.assert_called_once_with()
    write.assert_not_called()


def test_run_counts_separate_all_runs_from_outcomes_by_run_and_version() -> None:
    from jobs.workflow_verdicts import run_counts

    def run(run_id: str, version: str, state: str, cost: float | None, ended: str | None = "2026-10-02") -> Run:
        return Run(run_id, version, ("", "", ""), "2026-10-01", ended, state, cost=cost)

    history = WorkflowHistory("demo", None, (
        run("a", "v1", "accepted", 1.0), run("b", "v1", "failed", None),
        run("c", "v2", "accepted", None), run("d", "v3", "accepted", 2.0),
        run("open", "v3", "active", None, None)))

    assert run_counts(Snapshot("2026-10-20", (history,))) == {
        "settled": 4, "priced": 2, "outcomes": 3, "outcomes_priced": 2,
        "versions": 3, "versions_priced": 1, "versions_outcomes_priced": 2}


def test_daily_verdict_batch_records_and_wakes_only_when_enabled(monkeypatch) -> None:
    from jobs import workflow_verdicts

    db = MagicMock()
    evaluation = SimpleNamespace(verdict=SimpleNamespace(word="keep", reason="quality_better"))
    write = MagicMock(return_value=1)

    @contextmanager
    def transaction(*_args, **_kwargs):
        yield db

    monkeypatch.setattr(workflow_verdicts, "get_db", lambda: db)
    monkeypatch.setattr(workflow_verdicts, "read_open_snapshot", lambda *_args, **_kwargs: _snapshot())
    monkeypatch.setattr(workflow_verdicts, "price_snapshot", lambda _db, snapshot: snapshot)
    monkeypatch.setattr(workflow_verdicts, "compute", lambda _snapshot: [evaluation])
    monkeypatch.setattr(workflow_verdicts, "evaluate_trials", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(workflow_verdicts.evaluator, "VERDICT_WRITES_ENABLED", True)
    monkeypatch.setattr(workflow_verdicts, "write_transaction", transaction)
    monkeypatch.setattr(workflow_verdicts.evaluator, "write_verdicts", write)

    report = workflow_verdicts.run_daily()

    assert report.recorded == 1
    write.assert_called_once_with(db, [evaluation], wake_owners=True)


def test_a_workflow_version_with_runs_and_no_events_is_planned_once_and_a_reached_one_drops_out(db, monkeypatch) -> None:
    from core import workflow_verdict_evaluator as evaluator
    from jobs import workflow_verdicts

    node(db, "run")
    node(db, "old", version="v0", created="2026-09-01T00:00:00Z", ended="2026-09-02T00:00:00Z")
    db.execute("INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,ts,observed_ts,subject_key,"
               "attributes) VALUES ('r','r','measurement.verdict.reached','corsia','instrumented','2026-10-01',"
               "'2026-10-01','workflow:sdlc-process@v0','{}')")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM outcome_events WHERE event_name='measurement.verdict.planned'").fetchone()[0] == 0
    monkeypatch.setattr(evaluator, "VERDICT_WRITES_ENABLED", True)

    for _ in range(2):
        report = workflow_verdicts.run_daily(now=NOW)
        assert report.open_subjects == report.evaluated == 1
    rows = db.execute("SELECT subject_key FROM outcome_events WHERE event_name='measurement.verdict.planned'").fetchall()
    assert [row["subject_key"] for row in rows] == ["workflow:sdlc-process@v1"]


@pytest.mark.asyncio
async def test_nightly_runs_verdict_batch_after_successful_maintenance(monkeypatch) -> None:
    from automation import managed_process_events
    from core import thread_lifecycle
    from infra import request_timing
    from jobs import nightly

    record = MagicMock()
    verdicts = AsyncMock()
    reaper = AsyncMock()
    monkeypatch.setattr(nightly, "_claim_today", lambda _day: True)
    monkeypatch.setattr(nightly, "_record_maintenance_terminal", record)
    monkeypatch.setattr(thread_lifecycle, "run_lifecycle_retention", MagicMock())
    monkeypatch.setattr(request_timing, "prune_retention", MagicMock())
    monkeypatch.setattr(managed_process_events, "drain_retirements", MagicMock())
    monkeypatch.setattr(nightly, "_run_workflow_verdicts_safely", verdicts)
    monkeypatch.setattr(nightly, "_reap_worktrees_safely", reaper)

    await nightly._run_job_safely()

    assert record.call_args.args[1] == "ok"
    verdicts.assert_awaited_once_with()
    reaper.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_nightly_keeps_the_error_disposition_when_maintenance_fails(monkeypatch) -> None:
    from automation import managed_process_events
    from core import thread_lifecycle
    from infra import request_timing
    from jobs import nightly

    record = MagicMock()
    verdicts = AsyncMock()
    monkeypatch.setattr(nightly, "_claim_today", lambda _day: True)
    monkeypatch.setattr(nightly, "_record_maintenance_terminal", record)
    monkeypatch.setattr(thread_lifecycle, "run_lifecycle_retention", MagicMock(side_effect=RuntimeError("retention failed")))
    monkeypatch.setattr(request_timing, "prune_retention", MagicMock())
    monkeypatch.setattr(managed_process_events, "drain_retirements", MagicMock())
    monkeypatch.setattr(nightly, "_run_workflow_verdicts_safely", verdicts)
    monkeypatch.setattr(nightly, "_reap_worktrees_safely", AsyncMock())

    await nightly._run_job_safely()

    assert record.call_args.args[1] == "error"
    verdicts.assert_awaited_once_with()
