"""Arrival-driven correction recording for the verified local ONNX asset."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

from loguru import logger

from core.corrections_classifier import (
    AssetError, AssetManifest, Classification, ClassifierChild, ClassifierUnavailable,
    asset_root, load_pinned_manifest,
)
from core.corrections_recording import (
    LABEL_UNCLASSIFIABLE, CheckerVersion, CorrectionCandidate,
)
from infra.db_async import run_in_db
from jobs.corrections_scanning import PendingCandidateKey, hydrate_candidates

_QUEUE: asyncio.Queue[tuple[str, float]] = asyncio.Queue(maxsize=256)
_TASK: asyncio.Task[None] | None = None
_CHILD: ClassifierChild | None = None
_SCAN_OWED = False
_STOPPING = False
_STATE: dict[str, object] = {
    "state": "absent", "reason": "asset not verified", "retryable": True,
    "dropped_arrivals": 0, "queue_delay_ms": 0.0, "service_time_ms": 0.0,
}


def _clock() -> float:
    return time.monotonic()


def _cleanup_staging() -> None:
    """Discard an interrupted asset install; only a complete verified root is loadable."""
    root = asset_root()
    staging = root.with_name(f"{root.name}.staging")
    if staging.exists():
        shutil.rmtree(staging)


def _manifest() -> AssetManifest:
    manifest = load_pinned_manifest(asset_root())
    manifest.verify_files()
    return manifest


def _candidate(message_id: str) -> CorrectionCandidate | None:
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            """SELECT m.rowid,m.id,m.thread_id,t.project_id,m.created_at
               FROM messages m JOIN threads t ON t.id=m.thread_id WHERE m.id=?""", (message_id,),
        ).fetchone()
        if row is None:
            return None
        key = PendingCandidateKey(int(row[0]), str(row[1]), str(row[2]), row[3], str(row[4]))
    finally:
        db.close()
    candidates = hydrate_candidates([key])
    return candidates[0] if candidates else None


def _as_version(value) -> CheckerVersion:
    return CheckerVersion(value.asset_id, value.version, value.head, value.threshold, value.input_policy_id)


async def _record(candidate: CorrectionCandidate, result: Classification) -> bool:
    from jobs.corrections_recording import _record_fact

    classification = {
        "label": result.label,
        "truncated": result.truncated,
        "original_tokens": result.original_tokens,
        "failure_reason": "none",
    }
    return await run_in_db(_record_fact, candidate, classification, _as_version(result.version))


async def _record_unclassifiable(candidate: CorrectionCandidate, version: CheckerVersion, reason: str) -> bool:
    from jobs.corrections_recording import _record_fact

    return await run_in_db(
        _record_fact,
        candidate,
        {"label": LABEL_UNCLASSIFIABLE, "truncated": False, "original_tokens": 0, "failure_reason": reason},
        version,
    )


async def _classify(candidate: CorrectionCandidate) -> None:
    global _CHILD, _SCAN_OWED
    was_ready = _STATE.get("state") == "ready"
    needs_catch_up = not was_ready or _CHILD is None
    try:
        manifest = await asyncio.to_thread(_manifest)
    except AssetError as error:
        state = "absent" if not (asset_root() / "manifest.json").is_file() else "failed"
        _STATE.update({"state": state, "reason": str(error), "retryable": state == "absent"})
        logger.info("corrections classifier unavailable: {}", error)
        return
    _STATE.update({"state": "ready", "reason": None, "retryable": False, "version": manifest.version})
    if _CHILD is None:
        _CHILD = ClassifierChild(manifest)
    child = _CHILD
    version = _as_version(manifest.checker_version())
    for attempt in range(2):
        try:
            result = await asyncio.to_thread(child.classify, list(candidate.context), candidate.content)
            await _record(candidate, result)
            if needs_catch_up:
                _SCAN_OWED = True
            return
        except ClassifierUnavailable as error:
            logger.warning("corrections classifier attempt {} failed: {}", attempt + 1, error)
            _CHILD = None
            if attempt:
                await _record_unclassifiable(candidate, version, str(error).split(":", 1)[0])
                return
        except Exception:
            logger.exception("corrections classifier failed")
            return


async def _retry_pending_activation_notice() -> None:
    """Retry an accepted-delivery notice after restart or an interrupted mark."""
    from core.corrections_recording import active_corrections_version
    from jobs.corrections_activation import tell_conductor_about_activation

    version = await run_in_db(active_corrections_version)
    if version is not None:
        await tell_conductor_about_activation(version)


async def _activate_after_backfill(manifest: AssetManifest) -> None:
    """Switch Measurement only after this immutable version's bounded fill completes."""
    from api.measurement_agents import BASELINE_PERIODS
    from jobs.corrections_activation import (
        complete_backfill, measurement_window_start, tell_conductor_about_activation,
        weekly_correction_figure,
    )
    from core.corrections_recording import active_corrections_version
    from jobs.corrections_recording import _owner_user_ids

    owners = await run_in_db(_owner_user_ids)
    if len(owners) != 1:
        logger.warning("corrections activation skipped: expected one durable owner, found {}", len(owners))
        return
    version = manifest.checker_version()
    weeks = BASELINE_PERIODS + 1
    since = measurement_window_start().isoformat()
    old_version = await run_in_db(active_corrections_version)
    before = (
        await run_in_db(weekly_correction_figure, old_version, since=since, weeks=weeks)
        if old_version is not None else None
    )
    after = await run_in_db(weekly_correction_figure, version, since=since, weeks=weeks)
    activated = await run_in_db(
        complete_backfill,
        version,
        owner_user_id=owners[0],
        weeks=weeks,
        before_count=before,
        after_count=after,
    )
    if activated:
        await tell_conductor_about_activation(version)
    await _retry_pending_activation_notice()


async def _catch_up_scan() -> None:
    """Read the bounded window, yielding it whenever a live arrival is waiting."""
    global _SCAN_OWED
    from datetime import timezone
    from jobs.corrections_activation import measurement_window_start
    from jobs.corrections_recording import _next_pending_batch

    try:
        manifest = await asyncio.to_thread(_manifest)
    except AssetError as error:
        logger.info("corrections classifier scan deferred: {}", error)
        return
    cutoff = measurement_window_start().astimezone(timezone.utc)
    before = None
    while _QUEUE.empty():
        batch = await _next_pending_batch(
            _as_version(manifest.checker_version()), 20, before, since=cutoff.isoformat(),
        )
        if not batch.selected_keys:
            await _activate_after_backfill(manifest)
            return
        before = (batch.selected_keys[-1].created_at, batch.selected_keys[-1].message_id)
        current = []
        for candidate in batch.candidates:
            try:
                created = candidate.created_at.replace("Z", "+00:00")
                from datetime import datetime
                if datetime.fromisoformat(created).astimezone(timezone.utc) < cutoff:
                    return
            except ValueError:
                logger.warning("corrections classifier skipped candidate with invalid timestamp")
                continue
            current.append(candidate)
        for candidate in current:
            if not _QUEUE.empty():
                _SCAN_OWED = True
                return
            started_at = _clock()
            await _classify(candidate)
            elapsed = _clock() - started_at
            if not _QUEUE.empty():
                _SCAN_OWED = True
                return
            await asyncio.sleep(elapsed)
    if not _QUEUE.empty():
        _SCAN_OWED = True


async def _process_arrival(message_id: str, enqueued_at: float) -> None:
    started_at = _clock()
    try:
        _STATE["queue_delay_ms"] = (started_at - enqueued_at) * 1000
        candidate = await run_in_db(_candidate, message_id)
        if candidate is not None:
            await _classify(candidate)
    finally:
        _STATE["service_time_ms"] = (_clock() - started_at) * 1000
        _QUEUE.task_done()


async def _worker() -> None:
    """One owner: arrivals first, then owed scan/backfill, never competing tasks."""
    global _SCAN_OWED
    while True:
        try:
            message_id, enqueued_at = _QUEUE.get_nowait()
        except asyncio.QueueEmpty:
            if not _SCAN_OWED:
                return
            _SCAN_OWED = False
            await _catch_up_scan()
            continue
        await _process_arrival(message_id, enqueued_at)


def notify(message_id: str) -> None:
    """Best-effort post-admission trigger; it can never delay or fail a send."""
    global _TASK, _SCAN_OWED
    if _STOPPING:
        # The next startup's catch-up scan reads this message from the database.
        _SCAN_OWED = True
        return
    try:
        _QUEUE.put_nowait((message_id, _clock()))
        if _TASK is None or _TASK.done():
            _TASK = asyncio.create_task(_worker())
    except asyncio.QueueFull:
        _SCAN_OWED = True
        dropped = _STATE.get("dropped_arrivals")
        _STATE["dropped_arrivals"] = (dropped if isinstance(dropped, int) else 0) + 1
        logger.warning("corrections classifier arrival queue is full; scan is owed")
    except Exception:
        logger.exception("corrections classifier admission notification failed")


async def _startup_worker() -> None:
    await _retry_pending_activation_notice()
    await _worker()


def startup_scan() -> None:
    """Discard interrupted staging and start the one arrival-first worker."""
    global _TASK, _SCAN_OWED, _STOPPING
    _STOPPING = False
    _cleanup_staging()
    _SCAN_OWED = True
    if _TASK is None or _TASK.done():
        _TASK = asyncio.create_task(_startup_worker())


async def shutdown() -> None:
    """Cancel and join the worker, then stop the model child; later arrivals wait for the next startup."""
    global _TASK, _CHILD, _STOPPING
    _STOPPING = True
    task, _TASK = _TASK, None
    if task is not None:
        task.cancel()
        await asyncio.wait([task])
    child, _CHILD = _CHILD, None
    if child is not None:
        await asyncio.to_thread(child.close)


def state_snapshot() -> dict[str, object]:
    """Return the visible local-asset state without loading a model on demand."""
    return {**_STATE, "queue_depth": _QUEUE.qsize(), "scan_owed": _SCAN_OWED}


def notify_if_owner(message_id: str, *, role: str, content: str, metadata: object, duplicate: bool) -> None:
    from core.corrections_recording import is_owner_authored_message

    if not duplicate and is_owner_authored_message(role=role, content=content, metadata=metadata):
        notify(message_id)
