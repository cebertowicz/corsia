"""Arrival-driven correction recording for the verified local ONNX asset.

One owner, ``CorrectionsWorker`` (the module singleton ``WORKER``), holds all worker state.
Every classifier state change goes through ``CorrectionsWorker._set``, which logs it and
updates the snapshot; the scan status changes only through ``_owe``, ``_hold`` and
``_finish_scan``.

Classifier state (``state``)::

    from                         event                                              to
    any                          manifest.json missing                              absent
    any                          manifest unpinned, malformed, wrong size/checksum  failed (reinstall)
    absent|failed                manifest verified before any child has run         unavailable ("not
                                                                                    run yet")
    unavailable                  manifest verified, a child starts                  loading
    loading                      child replied ready (self-test passed) and a call  ready
                                 succeeded
    loading|ready                two load failures for one message: load timeout,   unavailable (reason
                                 self-test mismatch, memory ceiling while loading,  kept; retried at the
                                 exit before any reply                              next arrival/startup)
    ready                        newer child passes startup and replaces old child  ready
    ready                        newer child deterministic startup failure           ready (old child serves;
                                                                                    failed version is recorded)
    loading|ready                two failures of a loaded child on one input        unchanged; an
                                 (timeout, rss, child_error)                        ``unreadable`` fact

``ready`` is never set by manifest verification alone. An outage records nothing and
leaves the message pending; an unreadable input is an outcome, recorded with its
``failure_reason``, and the worker moves on. A deterministic failure is retained per
version, so a later owner message never starts the same broken child again.

Scan status (``scan``: idle | owed | running, plus ``held_reason``)::

    from      event                                                              to
    idle      startup; an arrival not queued (queue full, stopping); the         owed
              classifier becomes ready from another state; the worker task died
    owed      the arrival queue is empty and nothing holds the scan              running
    running   the pass found nothing pending and activation ran                  idle
    running   a live arrival is waiting                                          owed
    running   outage, absent or failed asset, pass error, corrupt activation     owed, held_reason
    held      an arrival is recorded (scored or unreadable), or startup          owed, hold cleared

``owed`` is cleared only when a pass finishes, so an error can delay the scan but never
lose it. Failed-state notices use a deterministic version-and-reason delivery key, so
restarts can retry delivery without repeating an accepted owner notice.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from loguru import logger

from core.corrections_child import ClassifierChild
from core.corrections_classifier import AssetError, AssetManifest, cleanup_staging
from core.corrections_recording import ClassifierVersion, CorrectionCandidate
from jobs.corrections_assets import AssetSelectionError, installed as _installed, select_manifest, version_number
from infra.db_async import run_in_db
from jobs.corrections_activation import activate_after_backfill, retry_pending_activation_notice
from jobs.corrections_recording import (
    candidate_for_message, record_classification, record_unreadable, unreadable_messages,
)
from jobs.corrections_runtime import classify

QUEUE_SIZE = 256
SCAN_BATCH = 20
ATTEMPTS_PER_KIND = 2


def _clock() -> float:
    return time.monotonic()


class CorrectionsWorker:
    """The one owner of the arrival queue, the model child and both state machines."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, float]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.arrived = asyncio.Event()
        self.call_seconds = 0.0
        self.task: asyncio.Task[None] | None = None
        self.child: ClassifierChild | None = None
        self.child_version: str | None = None
        self.children: dict[str, ClassifierChild] = {}
        self.active_manifest: AssetManifest | None = None
        self.stopping = False
        self.state = "absent"
        self.reason: str | None = "asset not verified"
        self.reader_reason: str | None = "The corrections classifier is not installed."
        self.version: ClassifierVersion | None = None
        self.versions: list[dict[str, str | None]] = []
        self.failed_versions: dict[str, str] = {}
        self.failure_notice_tasks: set[asyncio.Task] = set()
        self.scan = "idle"
        self.held_reason: str | None = None
        self.dropped_arrivals = 0
        self.queue_delay_ms = 0.0
        self.service_time_ms = 0.0

    # ---- state ---------------------------------------------------------------------------

    def _set(self, state: str, reason: str | None) -> None:
        if (state, reason) != (self.state, self.reason):
            logger.info("corrections classifier {} -> {}: {}", self.state, state, reason or "-")
        previous, previous_reason = self.state, self.reason
        self.state, self.reason = state, reason
        if state == "ready" and previous != "ready":
            # Unrecorded work must not reach activation; becoming ready owes the catch-up scan.
            self._owe()
        if state == "failed" and (previous, previous_reason) != (state, reason):
            self._notice_failed_state()

    def _owe(self) -> None:
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
        """Return the classifier state, scan status and arrival-queue figures."""
        return {
            "state": self.state,
            "reason": self.reason,
            "reader_reason": self.reader_reason,
            "retryable": self.state != "failed",
            "version": self.version.version if self.version else None,
            "selected_version": self.version.version if self.version else None,
            "versions": self.versions,
            "scan": self.scan,
            "scan_owed": self.scan != "idle",
            "held_reason": self.held_reason,
            "queue_depth": self.queue.qsize(),
            "dropped_arrivals": self.dropped_arrivals,
            "queue_delay_ms": self.queue_delay_ms,
            "service_time_ms": self.service_time_ms,
        }

    # ---- one message ---------------------------------------------------------------------

    async def _verified_manifest(self) -> AssetManifest | None:
        try:
            manifest, self.versions = await asyncio.to_thread(select_manifest)
        except AssetSelectionError as error:
            self.versions = error.versions
            self.reader_reason = error.reader or str(error)
            self._set("failed" if _installed() else "absent", str(error))
            return None
        except AssetError as error:
            self.versions = []
            self.reader_reason = error.reader or str(error)
            self._set("failed" if _installed() else "absent", str(error))
            return None
        reason = self.failed_versions.get(manifest.version)
        if reason is not None:
            self._mark_version_failed(manifest.version, reason)
            if self.active_manifest is not None:
                return self.active_manifest
            self.reader_reason = reason
            self._set("failed", reason)
            return None
        if self.state in ("absent", "failed"):
            self.reader_reason = None
            self._set("unavailable", "asset verified; the model has not run yet")
        return manifest

    def _mark_version_failed(self, version: str, reason: str) -> None:
        """Retain a deterministic startup failure on the worker, not in a module cache."""
        self.failed_versions[version] = reason
        self.versions = [
            {**item, "state": "failed", "reason": reason} if item["version"] == version else item
            for item in self.versions
        ]

    def _notice_failed_state(self) -> None:
        """Route one owner-visible failure notice through the durable update-notice path."""
        from automation.update_failure_notice import tell_corrections_classifier_failure_detached

        failed = next((item for item in self.versions if item["state"] == "failed"), None)
        version = self.failed_versions and next(reversed(self.failed_versions))
        version = version or (failed or {}).get("version") or (failed or {}).get("location") or "unknown"
        reason = self.reason or "classifier asset failed"
        reader = self.reader_reason or reason
        tell_corrections_classifier_failure_detached(
            str(version), reason, reader, tasks=self.failure_notice_tasks,
        )

    async def _classify(self, candidate: CorrectionCandidate) -> bool:
        """Record one candidate, or leave it pending on an outage."""
        return await classify(
            self,
            candidate,
            attempts_per_kind=ATTEMPTS_PER_KIND,
            child_factory=ClassifierChild,
            clock=_clock,
            record_classification=record_classification,
            record_unreadable=record_unreadable,
        )

    # ---- worker loop ---------------------------------------------------------------------

    async def _arrival(self, message_id: str, enqueued_at: float) -> None:
        started_at = _clock()
        self.queue_delay_ms = (started_at - enqueued_at) * 1000
        try:
            candidate = await run_in_db(candidate_for_message, message_id)
            if candidate is not None and await self._classify(candidate):
                self._release_hold()
        except Exception:
            logger.exception("corrections classifier arrival failed; the scan will read it")
            self._owe()
        finally:
            self.service_time_ms = (_clock() - started_at) * 1000
            self.queue.task_done()

    async def _scan_pass(self) -> None:
        """Read the bounded window, yielding to any live arrival; never raises."""
        self.scan = "running"
        try:
            outcome = await self._scan()
        except Exception as error:
            logger.exception("corrections scan pass failed")
            self._hold(f"scan error: {type(error).__name__}")
            return
        if outcome == "done":
            self._finish_scan()
        elif outcome == "yielded":
            self.scan = "owed"
        else:
            self._hold(outcome)

    async def _scan(self) -> str:
        """Return ``done``, ``yielded`` or the reason the scan is held."""
        from datetime import timezone

        from jobs.corrections_activation import measurement_window_start
        from jobs.corrections_recording import next_pending_batch

        manifest = await self._verified_manifest()
        if manifest is None:
            return f"classifier {self.state}"
        cutoff = measurement_window_start().astimezone(timezone.utc)
        version = manifest.classifier_version()
        before = None
        while self.queue.empty():
            batch = await next_pending_batch(version, SCAN_BATCH, before, since=cutoff.isoformat())
            if not batch.selected_keys:
                # Unrecorded work must not reach activation.
                return await activate_after_backfill(version)
            before = (batch.selected_keys[-1].created_at, batch.selected_keys[-1].message_id)
            for candidate in batch.candidates:
                if not self.queue.empty():
                    return "yielded"
                if not await self._classify(candidate):
                    return f"classifier {self.state}: {self.reason}"
                self.arrived.clear()
                if not self.queue.empty():
                    return "yielded"
                # Pause one call's duration (a cold load not counted); a queued arrival ends it at once.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.arrived.wait(), self.call_seconds)
        return "yielded"

    async def run(self) -> None:
        """One owner: arrivals first, then the owed scan, never competing tasks."""
        while True:
            try:
                message_id, enqueued_at = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                if self.scan == "idle" or self.held_reason is not None:
                    return
                await self._scan_pass()
                continue
            await self._arrival(message_id, enqueued_at)

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

    # ---- lifecycle -----------------------------------------------------------------------

    def notify(self, message_id: str) -> None:
        """Queue one arrival and start the worker; a full queue or stopping app owes the scan."""
        if self.stopping:
            # The next startup's catch-up scan reads this message from the database.
            self._owe()
            return
        try:
            self.queue.put_nowait((message_id, _clock()))
        except asyncio.QueueFull:
            self._owe()
            self.dropped_arrivals += 1
            logger.warning("corrections classifier arrival queue is full; scan is owed")
            return
        self.arrived.set()
        self._start_task(self.run())

    def startup(self) -> None:
        """Discard interrupted staging, release any hold, owe the scan and start the worker."""
        self.stopping = False
        cleanup_staging()
        self._release_hold()
        self.scan = "owed"
        self._start_task(_startup_worker())

    async def shutdown(self) -> None:
        """Mark stopping, cancel and join the worker, then kill and reap the model child."""
        if self.task is not None and self.task.get_loop() is not asyncio.get_running_loop():
            # Waiting on another loop's task never finishes; refuse loudly like the other owned tasks.
            raise RuntimeError("corrections classifier worker is attached to a different loop")
        self.stopping = True
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            await asyncio.wait([task])
        children, self.children = list(self.children.values()), {}
        self.child = None
        self.child_version = None
        self.active_manifest = None
        for child in children:
            # A load or call may still run in a worker thread: kill without its lock, then reap.
            child.kill()
            await asyncio.to_thread(child.close)

WORKER = CorrectionsWorker()

async def _startup_worker() -> None:
    try:
        await retry_pending_activation_notice()
    except Exception:
        logger.exception("corrections activation notice retry failed; the next startup retries it")
    await WORKER.run()

def notify(message_id: str) -> None:
    """Queue one arrival for the worker; see ``notify_if_owner`` for the send-path guard."""
    WORKER.notify(message_id)

def startup_scan() -> None:
    """Discard interrupted staging and start the one arrival-first worker."""
    WORKER.startup()

async def shutdown() -> None:
    """Cancel and join the worker, then stop the model child; later arrivals wait for the next startup."""
    await WORKER.shutdown()

def state_snapshot(db: Any = None) -> dict[str, object]:
    """Return state and, with a DB, the current version's unreadable-message count."""
    snapshot = WORKER.snapshot()
    if db is not None:
        snapshot["unreadable_messages"] = unreadable_messages(db, WORKER.version) if WORKER.version else 0
    return snapshot

def notify_if_owner(accepted: Any, *, role: str, content: str, metadata: object) -> None:
    """Best-effort post-admission trigger for one ``AcceptedInput``; it can never delay or fail a send."""
    from core.corrections_recording import is_owner_authored_message

    try:
        if not accepted.duplicate and is_owner_authored_message(role=role, content=content, metadata=metadata):
            notify(accepted.message_id)
    except Exception:
        logger.exception("corrections classifier admission notification failed")
