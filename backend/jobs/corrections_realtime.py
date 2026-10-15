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
    ready                        child restarted after idle exit or a message       ready
                                 failure, and the restart loaded
    loading|ready                two failures of a loaded child on one input        unchanged; an
                                 (timeout, rss, child_error)                        ``unreadable`` fact

``ready`` is never set by manifest verification alone. An outage records nothing and
leaves the message pending; an unreadable input is an outcome, recorded with its
``failure_reason``, and the worker moves on.

Scan status (``scan``: idle | owed | running, plus ``held_reason``)::

    from      event                                                              to
    idle      startup; an arrival not queued (queue full, stopping); the         owed
              classifier becomes ready from another state; the worker task died
    owed      the arrival queue is empty and nothing holds the scan              running
    running   the pass found nothing pending and activation ran                  idle
    running   a live arrival is waiting                                          owed
    running   outage, absent or failed asset, pass error, corrupt activation      owed, held_reason
    held      an arrival is recorded (scored or unreadable), or startup          owed, hold cleared

``owed`` is cleared only when a pass finishes, so an error can delay the scan but never
lose it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from core.corrections_child import ClassifierChild, ClassifierUnavailable, MessageFailed
from core.corrections_classifier import AssetError, AssetManifest, asset_root, cleanup_staging, load_pinned_manifest
from core.corrections_recording import LABEL_UNREADABLE, CorrectionCandidate
from infra.db_async import run_in_db
from jobs.corrections_activation import activate_after_backfill, retry_pending_activation_notice
from jobs.corrections_recording import candidate_for_message, record_classification, record_unreadable

QUEUE_SIZE = 256
SCAN_BATCH = 20
ATTEMPTS_PER_KIND = 2


def _clock() -> float:
    return time.monotonic()


def _manifest() -> AssetManifest:
    manifest = load_pinned_manifest(asset_root())
    manifest.verify_files()
    return manifest


class CorrectionsWorker:
    """The one owner of the arrival queue, the model child and both state machines."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, float]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.task: asyncio.Task[None] | None = None
        self.child: ClassifierChild | None = None
        self.stopping = False
        self.state = "absent"
        self.reason: str | None = "asset not verified"
        self.version: str | None = None
        self.scan = "idle"
        self.held_reason: str | None = None
        self.dropped_arrivals = 0
        self.queue_delay_ms = 0.0
        self.service_time_ms = 0.0

    # ---- state ---------------------------------------------------------------------------

    def _set(self, state: str, reason: str | None) -> None:
        if (state, reason) != (self.state, self.reason):
            logger.info("corrections classifier {} -> {}: {}", self.state, state, reason or "-")
        previous, self.state, self.reason = self.state, state, reason
        if state == "ready" and previous != "ready":
            # Arrivals left pending while it was not ready are read by the owed scan.
            self._owe()

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
        return {
            "state": self.state,
            "reason": self.reason,
            "retryable": self.state != "failed",
            "version": self.version,
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
            manifest = await asyncio.to_thread(_manifest)
        except AssetError as error:
            present = (asset_root() / "manifest.json").is_file()
            self._set("failed" if present else "absent", str(error))
            return None
        self.version = manifest.version
        if self.state in ("absent", "failed"):
            self._set("unavailable", "asset verified; the model has not run yet")
        return manifest

    async def _drop_child(self) -> None:
        """Stop the current child; forget it only once it has exited, so shutdown still sees it."""
        child = self.child
        if child is None:
            return
        await asyncio.to_thread(child.close)
        if self.child is child:
            self.child = None

    async def _classify(self, candidate: CorrectionCandidate) -> bool:
        """Record one candidate; True when a fact, scored or unreadable, was written.

        False means an outage: nothing was recorded and the message stays pending.
        """
        manifest = await self._verified_manifest()
        if manifest is None:
            return False
        outages: list[str] = []
        failures: list[str] = []
        while len(outages) < ATTEMPTS_PER_KIND and len(failures) < ATTEMPTS_PER_KIND:
            if self.child is None:
                self.child = ClassifierChild(manifest)
            child = self.child
            if self.state != "ready" and not (child.loaded and child.running):
                self._set("loading", "starting the model and its self-test")
            try:
                await asyncio.to_thread(child.ensure_loaded)
            except ClassifierUnavailable as error:
                outages.append(str(error))
                logger.warning("corrections classifier did not load: {}", error)
                await self._drop_child()
                continue
            try:
                result = await asyncio.to_thread(child.classify, list(candidate.context), candidate.content)
            except MessageFailed as error:
                failures.append(error.reason)
                logger.warning("corrections classifier failed on one input: {}", error.reason)
                await self._drop_child()
                continue
            except ClassifierUnavailable as error:
                outages.append(str(error))
                await self._drop_child()
                continue
            await record_classification(candidate, result)
            self._set("ready", None)
            return True
        if len(failures) == ATTEMPTS_PER_KIND:
            await record_unreadable(candidate, manifest.classifier_version(), failures[-1])
            return True
        self._set("unavailable", outages[-1])
        return False

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
        from jobs.corrections_recording import _next_pending_batch

        manifest = await self._verified_manifest()
        if manifest is None:
            return f"classifier {self.state}"
        cutoff = measurement_window_start().astimezone(timezone.utc)
        version = manifest.classifier_version()
        before = None
        while self.queue.empty():
            batch = await _next_pending_batch(version, SCAN_BATCH, before, since=cutoff.isoformat())
            if not batch.selected_keys:
                return await activate_after_backfill(version)
            before = (batch.selected_keys[-1].created_at, batch.selected_keys[-1].message_id)
            for candidate in batch.candidates:
                if not self.queue.empty():
                    return "yielded"
                started_at = _clock()
                if not await self._classify(candidate):
                    # Unrecorded work must not reach activation.
                    return f"classifier {self.state}: {self.reason}"
                elapsed = _clock() - started_at
                if not self.queue.empty():
                    return "yielded"
                await asyncio.sleep(elapsed)
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
        # The next notify or startup restarts the task; the scan stays owed until a pass finishes.
        self._hold(f"worker error: {type(error).__name__}")

    # ---- lifecycle -----------------------------------------------------------------------

    def notify(self, message_id: str) -> None:
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
        self._start_task(self.run())

    def startup(self) -> None:
        self.stopping = False
        cleanup_staging()
        self._release_hold()
        self.scan = "owed"
        self._start_task(_startup_worker())

    async def shutdown(self) -> None:
        if self.task is not None and self.task.get_loop() is not asyncio.get_running_loop():
            # Waiting on another loop's task never finishes; refuse loudly like the other owned tasks.
            raise RuntimeError("corrections classifier worker is attached to a different loop")
        self.stopping = True
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            await asyncio.wait([task])
        child, self.child = self.child, None
        if child is not None:
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
    """Return the visible local-asset state without loading a model on demand.

    With a database connection it also counts the recorded ``unreadable`` messages.
    """
    snapshot = WORKER.snapshot()
    if db is not None:
        from core.corrections_recording import CORRECTION_EVENT

        row = db.execute(
            """SELECT COUNT(*) FROM outcome_events
               WHERE event_name=? AND retracted_at IS NULL AND json_extract(attributes,'$.label')=?""",
            (CORRECTION_EVENT, LABEL_UNREADABLE),
        ).fetchone()
        snapshot["unreadable_messages"] = int(row[0])
    return snapshot


def notify_if_owner(accepted: Any, *, role: str, content: str, metadata: object) -> None:
    """Best-effort post-admission trigger for one ``AcceptedInput``; it can never delay or fail a send."""
    from core.corrections_recording import is_owner_authored_message

    try:
        if not accepted.duplicate and is_owner_authored_message(role=role, content=content, metadata=metadata):
            notify(accepted.message_id)
    except Exception:
        logger.exception("corrections classifier admission notification failed")
