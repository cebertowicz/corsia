"""Arrival-driven correction recording with one worker-owned classifier state machine."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from loguru import logger

from core.corrections_child import ClassifierChild, ClassifierUnavailable, MessageFailed
from core.corrections_classifier import AssetError, AssetManifest, cleanup_staging
from core.corrections_recording import ClassifierVersion, CorrectionCandidate
from jobs.corrections_assets import installed as _installed, select_manifest, version_number
from infra.db_async import run_in_db
from jobs.corrections_activation import activate_after_backfill, retry_pending_activation_notice
from jobs.corrections_recording import (
    candidate_for_message, record_classification, record_unreadable, unreadable_messages,
)

QUEUE_SIZE = 256
SCAN_BATCH = 20
ATTEMPTS_PER_KIND = 2
_LAST_VERSIONS: list[dict[str, str | None]] = []

def _clock() -> float:
    return time.monotonic()

def _manifest() -> AssetManifest:
    global _LAST_VERSIONS
    try:
        manifest, _LAST_VERSIONS = select_manifest()
        return manifest
    except AssetError:
        # Preserve the selection diagnostics for the system state snapshot.
        try:
            _, _LAST_VERSIONS = select_manifest()
        except AssetError:
            pass
        raise

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
        self.version: ClassifierVersion | None = None
        self.versions: list[dict[str, str | None]] = []
        self.failed_version: str | None = None
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
        """Return the classifier state, scan status and arrival-queue figures."""
        return {
            "state": self.state,
            "reason": self.reason,
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
            selected = await asyncio.to_thread(_manifest)
        except AssetError as error:
            self.versions = _LAST_VERSIONS
            self._set("failed" if _installed() else "absent", str(error))
            return None
        manifest = selected
        self.versions = _LAST_VERSIONS or [
            {"location": manifest.path.parent.name if hasattr(manifest, "path") else "test", "version": manifest.version, "state": "ready", "reason": None}
        ]
        if self.failed_version == manifest.version:
            reason = f"asset {manifest.version} failed deterministic startup checks"
            self.versions = [
                {**item, "state": "failed", "reason": reason} if item["version"] == manifest.version else item
                for item in self.versions
            ]
            if self.active_manifest is not None:
                return self.active_manifest
            self._set("failed", reason)
            return None
        if self.failed_version is not None:
            self.failed_version = None
        self.versions = _LAST_VERSIONS or [
            {"location": manifest.path.parent.name if hasattr(manifest, "path") else "test", "version": manifest.version, "state": "ready", "reason": None}
        ]
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
            self.child_version = None
        self.children = {key: value for key, value in self.children.items() if value is not child}

    async def _classify(self, candidate: CorrectionCandidate) -> bool:
        """Record one candidate, or leave it pending on an outage."""
        manifest = await self._verified_manifest()
        if manifest is None:
            return False
        outages: list[str] = []
        failures: list[str] = []
        self.call_seconds = 0.0
        fallback_child = self.child if self.child_version != manifest.version else None
        fallback_version = self.child_version if fallback_child is not None else None
        if fallback_child is not None:
            self.child = ClassifierChild(manifest)
            self.child_version = manifest.version
            self.children[manifest.version] = self.child
        while len(outages) < ATTEMPTS_PER_KIND and len(failures) < ATTEMPTS_PER_KIND:
            if self.child is None:
                self.child = ClassifierChild(manifest)
                self.child_version = manifest.version
                self.children[manifest.version] = self.child
            child = self.child
            if self.state != "ready" and not (child.loaded and child.running):
                self._set("loading", "starting the model and its self-test")
            try:
                await asyncio.to_thread(child.ensure_loaded)
            except ClassifierUnavailable as error:
                reason = str(error)
                logger.warning("corrections classifier did not load: {}", error)
                await self._drop_child()
                if fallback_child is not None:
                    self.child, self.child_version = fallback_child, fallback_version
                    if reason.startswith("load error:"):
                        self.failed_version = manifest.version
                        return await self._classify(candidate)
                    self._set("ready", None)
                    return False
                if reason.startswith("load error:"):
                    self.failed_version = manifest.version
                    self._set("failed", reason)
                    return False
                outages.append(reason)
                continue
            if fallback_child is not None:
                await asyncio.to_thread(fallback_child.close)
                self.children = {key: value for key, value in self.children.items() if value is not fallback_child}
                fallback_child = None
            self.active_manifest = manifest
            self.version = manifest.classifier_version()
            call_started = _clock()
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
            finally:
                self.call_seconds += _clock() - call_started
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
                return await activate_after_backfill(version)
            before = (batch.selected_keys[-1].created_at, batch.selected_keys[-1].message_id)
            for candidate in batch.candidates:
                if not self.queue.empty():
                    return "yielded"
                if not await self._classify(candidate):
                    # Unrecorded work must not reach activation.
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
        # The next notify or startup restarts the task; the scan stays owed until a pass finishes.
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
