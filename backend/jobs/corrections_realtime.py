"""Arrival-driven correction recording for the verified local ONNX asset.

One ``CorrectionsWorker`` owns the arrival queue, exactly one model child, and the
manifest that child serves. Selection chooses the highest verified pinned manifest
not deterministically failed in this process. A call and a scan pass retain their
selected manifest; only the next arrival or scan pass can select a different one.

Classifier state (``state``)::

    from                         event                                      to
    any                          manifest.json missing                      absent
    any                          manifest unpinned/malformed/damaged        failed (reinstall)
    absent|failed                manifest verified before a child runs      unavailable ("not run yet")
    unavailable                  child starts its self-test                  loading
    loading                      child reply and classification succeed     ready
    loading|ready                two load/serve outages for one input       unavailable
    loading|ready                two native load exits, sole version        unavailable (back off; notify once)
    loading|ready                newer deterministic startup failure         excluded; next pinned version
                                                                         selected, ready if it loads
    loading|ready                two input failures                         ready; ``unreadable`` fact

Scan state (``scan``)::

    from                         event                                      to
    idle                         ready, startup, or dropped arrival          owed
    idle                         arrival error, or notify while stopping/no loop owed
    owed                         worker starts a pass                         running
    running                      pass finishes                                idle
    running                      yields to an arrival or must reselect        owed
    owed|running                 unavailable pass or worker error             held
    held                         recorded/unreadable arrival or startup        owed
                                 `_release_hold()`

``held`` is represented as ``scan == "owed"`` with ``held_reason`` set. ``owed`` clears
only when a scan pass finishes; a ``reselect`` result returns it to ``owed`` for the next pass.

An outage writes no fact and keeps the source pending. A deterministic startup
failure is retained per process in ``failed_versions``. A scan activates only after
it reaches the end using its originally selected manifest; a deterministic failure
ends the pass unactivated, and the next pass re-selects (or the state is failed).
A review unit's key (``UNIT_KINDS``) only writes the notes it owes; a pass that
reaches the end first recovers the unit keys the worker never took.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import timezone
from typing import Any

from loguru import logger

from core.corrections_classifier import CORRECTIONS_SPEC
from core.onnx_asset import AssetError, AssetManifest, cleanup_staging
from core.onnx_child import OnnxChild
from core.corrections_recording import ClassifierVersion, CorrectionCandidate
from infra.db_async import run_in_db
from jobs.corrections_activation import activate_after_backfill, retry_pending_activation_notice
from jobs.corrections_assets import AssetSelectionError, asset_root, installed as _installed, select_manifest
from jobs.corrections_recording import arrival_candidate, record_classification, record_unreadable, unreadable_facts
from jobs.corrections_runtime import (
    ClassificationOutcome, classify, drop_child, hand_to_app_loop, record_only_version_load_crash,
)

QUEUE_SIZE = 256
SCAN_BATCH = 20
ATTEMPTS_PER_KIND = 2
LOAD_CRASH_BACKOFF_SECONDS = 60.0


def _clock() -> float:
    return time.monotonic()


class CorrectionsWorker:
    """The one owner of the arrival queue, one child, and both state machines."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, str, float]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.arrived = asyncio.Event()
        self.call_seconds = 0.0
        self.task: asyncio.Task[None] | None = None
        self.child: OnnxChild | None = None
        self.child_manifest: AssetManifest | None = None
        self.stopping = False
        self.state = "absent"
        self.reason: str | None = "asset not verified"
        self.reader_reason: str | None = "The corrections classifier is not installed."
        self.version: ClassifierVersion | None = None
        self.versions: list[dict[str, str | None]] = []
        self.failed_versions: dict[str, str] = {}
        # A present version was already noticed; a positive value is its active retry deadline.
        self.load_crash_retry_at: dict[str, float] = {}
        self.failure_notice_tasks: set[asyncio.Task] = set()
        self.scan = "idle"
        self.held_reason: str | None = None
        self.dropped_arrivals = 0
        self.queue_delay_ms = 0.0
        self.service_time_ms = 0.0

    def _set(self, state: str, reason: str | None, *, failure_version: str | None = None) -> None:
        if (state, reason) != (self.state, self.reason):
            logger.info("corrections classifier {} -> {}: {}", self.state, state, reason or "-")
        previous, previous_reason = self.state, self.reason
        self.state, self.reason = state, reason
        if state == "ready" and previous != "ready":
            self._owe()
        if state == "failed" and (previous, previous_reason) != (state, reason):
            self._notice_failed_state(failure_version)

    def _owe(self) -> None:
        """Idle to owed: an arrival the queue could not take is left for the next pass."""
        if self.scan == "idle":
            self.scan = "owed"

    def _hold(self, reason: str) -> None:
        if self.held_reason != reason:
            logger.warning("corrections scan held: {}", reason)
        self.scan, self.held_reason = "owed", reason

    def _release_hold(self) -> None:
        if self.held_reason is not None:
            logger.info("corrections scan hold cleared (was: {})", self.held_reason)
        self.held_reason = None

    def _finish_scan(self) -> None:
        self.scan, self.held_reason = "idle", None

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self.state, "reason": self.reason, "reader_reason": self.reader_reason,
            "retryable": self.state != "failed", "version": self.version.version if self.version else None,
            "versions": self.versions, "scan": self.scan, "scan_owed": self.scan != "idle",
            "held_reason": self.held_reason, "queue_depth": self.queue.qsize(),
            "dropped_arrivals": self.dropped_arrivals, "queue_delay_ms": self.queue_delay_ms,
            "service_time_ms": self.service_time_ms,
        }

    async def _select_manifest(self, *, after_version_failure: bool = False) -> AssetManifest | None:
        try:
            if self.failed_versions:
                manifest, self.versions = await asyncio.to_thread(select_manifest, exclude=self.failed_versions)
            else:
                manifest, self.versions = await asyncio.to_thread(select_manifest)
        except AssetSelectionError as error:
            self.versions = error.versions
            self.reader_reason = error.reader or str(error)
            if self.failed_versions and self.state == "failed":
                return None
            if not after_version_failure:
                failed = next((item for item in self.versions
                               if item["state"] == "failed" and item.get("version") not in self.failed_versions), {})
                self._set(
                    "failed" if _installed() else "absent", str(error),
                    failure_version=failed.get("version") or failed.get("location"),
                )
            return None
        except AssetError as error:
            self.versions = []
            self.reader_reason = error.reader or str(error)
            if not after_version_failure:
                self._set("failed" if _installed() else "absent", str(error))
            return None
        if self.state in ("absent", "failed"):
            self.reader_reason = None
            self._set("unavailable", "asset verified; the model has not run yet")
        return manifest

    def _notice_failed_state(self, failure_version: str | None) -> None:
        from automation.update_failure_notice import tell_corrections_classifier_failure_detached

        version = failure_version or "unknown"
        reason = self.reason or "classifier asset failed"
        reader = self.reader_reason or reason
        tell_corrections_classifier_failure_detached(
            str(version), reason, reader, tasks=self.failure_notice_tasks,
        )

    async def _after_version_failure(self, failure: ClassificationOutcome) -> AssetManifest | None:
        """Re-select past a deterministic failure; with nothing left, fail naming that version."""
        manifest = await self._select_manifest(after_version_failure=True)
        if manifest is None:
            self.reader_reason = failure.reason
            self._set("failed", failure.reason, failure_version=failure.failure_version)
        return manifest

    async def _classify_with(self, candidate: CorrectionCandidate, manifest: AssetManifest) -> ClassificationOutcome:
        now = _clock()
        deadline = self.load_crash_retry_at.get(manifest.version)
        if deadline is not None and deadline > now:
            reason = "classifier keeps crashing on load; retrying after backoff"
            self._set("unavailable", reason)
            return ClassificationOutcome("outage", reason)
        has_fallback = any(
            item["state"] == "ready" and item["version"] != manifest.version
            for item in self.versions
        )
        outcome = await classify(
            self, candidate, manifest, attempts_per_kind=ATTEMPTS_PER_KIND, has_fallback=has_fallback,
            clock=_clock, record_classification=record_classification,
            record_unreadable=record_unreadable,
        )
        if outcome.only_version_load_crash:
            retry_at = now + LOAD_CRASH_BACKOFF_SECONDS
            record_only_version_load_crash(self, manifest.version, outcome.reason or "load exited", retry_at)
        elif outcome.load_succeeded and manifest.version in self.load_crash_retry_at:
            self.load_crash_retry_at[manifest.version] = 0.0
        return outcome

    async def _classify(
        self, candidate: CorrectionCandidate, manifest: AssetManifest | None = None,
    ) -> ClassificationOutcome:
        """Classify a scan's fixed manifest, or select (past failed versions) for an arrival."""
        if manifest is not None:
            return await self._classify_with(candidate, manifest)
        manifest = await self._select_manifest()
        if manifest is None:
            return ClassificationOutcome("outage", self.reason)
        while True:  # bounded: each version_failed adds one pinned version to failed_versions
            outcome = await self._classify_with(candidate, manifest)
            if outcome.kind != "version_failed":
                return outcome
            manifest = await self._after_version_failure(outcome)
            if manifest is None:
                return outcome

    async def _arrival(self, source_kind: str, source_id: str, enqueued_at: float) -> None:
        started_at = _clock()
        self.queue_delay_ms = (started_at - enqueued_at) * 1000
        try:
            candidate = await run_in_db(arrival_candidate, source_kind, source_id)
            if candidate is not None and (await self._classify(candidate)).kind in {"recorded", "unreadable"}:
                self._release_hold()
        except Exception:
            logger.exception("corrections classifier arrival failed; the scan will read it")
            self._owe()
        finally:
            self.service_time_ms = (_clock() - started_at) * 1000
            self.queue.task_done()

    async def _scan_pass(self) -> None:
        self.scan = "running"
        try:
            outcome = await self._scan()
        except Exception as error:
            logger.exception("corrections scan pass failed")
            self._hold(f"scan error: {type(error).__name__}")
            return
        if outcome == "done":
            self._finish_scan()
        elif outcome in ("yielded", "reselect"):
            self.scan = "owed"
        else:
            self._hold(outcome)

    async def _scan(self) -> str:
        """Walk and activate one immutable manifest's measurement window."""
        from jobs.corrections_activation import measurement_window_start
        from jobs.corrections_recording import next_pending_batch, recover_unit_notes

        manifest = await self._select_manifest()
        if manifest is None:
            return f"classifier {self.state}"
        cutoff = measurement_window_start().astimezone(timezone.utc)
        version = ClassifierVersion(**manifest.head_version(CORRECTIONS_SPEC.required_heads[0]))
        before = None
        while self.queue.empty():
            batch = await next_pending_batch(version, SCAN_BATCH, before, since=cutoff.isoformat())
            if not batch.selected_keys:
                await recover_unit_notes(cutoff.isoformat())  # the unit keys the worker never took
                return await activate_after_backfill(version)
            tail = batch.selected_keys[-1]
            before = (tail.created_at, tail.source_kind, tail.source_id)
            for candidate in batch.candidates:
                if not self.queue.empty():
                    return "yielded"
                outcome = await self._classify(candidate, manifest)
                if outcome.kind == "version_failed":
                    if await self._after_version_failure(outcome) is not None:
                        return "reselect"
                    return f"classifier failed: {outcome.reason}"
                if outcome.kind == "outage":
                    return f"classifier {self.state}: {outcome.reason or self.reason}"
                self.arrived.clear()
                if not self.queue.empty():
                    return "yielded"
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.arrived.wait(), self.call_seconds)
        return "yielded"

    async def run(self) -> None:
        while True:
            try:
                source_kind, source_id, enqueued_at = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                if self.scan == "idle" or self.held_reason is not None:
                    return
                await self._scan_pass()
                continue
            await self._arrival(source_kind, source_id, enqueued_at)

    def _start_task(self, coroutine) -> None:
        if self.task is not None and not self.task.done():
            coroutine.close()
            return
        self.task = asyncio.create_task(coroutine)
        self.task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        logger.opt(exception=error).error("corrections classifier worker stopped")
        if self.state == "loading":
            self._set("unavailable", f"worker error: {type(error).__name__}")
        self._hold(f"worker error: {type(error).__name__}")

    def notify(self, source_kind: str, source_id: str) -> None:
        """Take a source or unit key from any thread; off the loop it hops onto the application loop."""
        if self.stopping:
            self._owe()
            return
        if hand_to_app_loop(self, source_kind, source_id):
            return
        try:
            self.queue.put_nowait((source_kind, source_id, _clock()))
        except asyncio.QueueFull:
            self._owe()
            self.dropped_arrivals += 1
            logger.warning("corrections classifier arrival queue is full; scan is owed")
            return
        self.arrived.set()
        self._start_task(self.run())

    def startup(self) -> None:
        self.stopping = False
        cleanup_staging(asset_root())
        self._release_hold()
        self.scan = "owed"
        self._start_task(_startup_worker())

    async def shutdown(self) -> None:
        if self.task is not None and self.task.get_loop() is not asyncio.get_running_loop():
            raise RuntimeError("corrections classifier worker is attached to a different loop")
        self.stopping = True
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            await asyncio.wait([task])
        if self.child is not None:
            self.child.kill()
        await drop_child(self)


WORKER = CorrectionsWorker()


async def _startup_worker() -> None:
    try:
        await retry_pending_activation_notice()
    except Exception:
        logger.exception("corrections activation notice retry failed; the next startup retries it")
    await WORKER.run()


def notify(source_kind: str, source_id: str) -> None:
    WORKER.notify(source_kind, source_id)


def startup_scan() -> None:
    WORKER.startup()


async def shutdown() -> None:
    await WORKER.shutdown()


def state_snapshot(db: Any = None) -> dict[str, object]:
    snapshot = WORKER.snapshot()
    if db is not None:
        snapshot["unreadable_facts"] = unreadable_facts(db, WORKER.version) if WORKER.version else 0
    return snapshot


def notify_if_owner(accepted: Any, *, role: str, content: str, metadata: object) -> None:
    from core.corrections_recording import is_owner_authored_message

    try:
        if not accepted.duplicate and is_owner_authored_message(role=role, content=content, metadata=metadata):
            notify("message", accepted.message_id)
    except Exception:
        logger.exception("corrections classifier admission notification failed")
