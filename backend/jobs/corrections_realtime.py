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
    asset_root,
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
_STATE: dict[str, object] = {
    "state": "absent", "reason": "asset not verified", "retryable": True,
    "dropped_arrivals": 0, "queue_delay_ms": 0.0, "service_time_ms": 0.0,
}


def _cleanup_staging() -> None:
    """Discard an interrupted asset install; only a complete verified root is loadable."""
    root = asset_root()
    staging = root.with_name(f"{root.name}.staging")
    if staging.exists():
        shutil.rmtree(staging)


def _manifest() -> AssetManifest:
    root = asset_root()
    path = root / "manifest.json"
    if not path.is_file():
        raise AssetError("classifier asset is absent")
    # The production source pin is intentionally optional for local staged assets;
    # release builds populate it from the versioned pin module.
    from core.corrections_asset_pins import PINNED_MANIFESTS

    manifest = AssetManifest.load(path, expected_sha256=PINNED_MANIFESTS.get(path.parent.name))
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
    try:
        manifest = await asyncio.to_thread(_manifest)
    except AssetError as error:
        state = "absent" if not (asset_root() / "manifest.json").is_file() else "failed"
        _STATE.update({"state": state, "reason": str(error), "retryable": state == "absent"})
        logger.info("corrections classifier unavailable: {}", error)
        return
    _STATE.update({"state": "ready", "reason": None, "retryable": False, "version": manifest.version})
    if not was_ready:
        _SCAN_OWED = True
    if _CHILD is None:
        _CHILD = ClassifierChild(manifest)
    child = _CHILD
    version = _as_version(manifest.checker_version())
    for attempt in range(2):
        try:
            result = await asyncio.to_thread(child.classify, list(candidate.context), candidate.content)
            await _record(candidate, result)
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


async def _activate_after_backfill(manifest: AssetManifest) -> None:
    """Switch Measurement only after this immutable version's bounded fill completes."""
    from api.measurement_agents import BASELINE_PERIODS
    from jobs.corrections_activation import (
        active_correction_count, complete_backfill, count_corrections,
        tell_conductor_about_activation,
    )
    from jobs.corrections_recording import _owner_user_ids

    owners = await run_in_db(_owner_user_ids)
    if len(owners) != 1:
        logger.warning("corrections activation skipped: expected one durable owner, found {}", len(owners))
        return
    version = manifest.checker_version()
    activated = await run_in_db(
        complete_backfill,
        version,
        owner_user_id=owners[0],
        weeks=BASELINE_PERIODS + 1,
        before_count=await run_in_db(active_correction_count),
        after_count=await run_in_db(count_corrections, version),
    )
    if activated:
        await tell_conductor_about_activation(version)


async def _catch_up_scan() -> None:
    """Queue only the bounded Measurement window after startup or a dropped arrival."""
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
                return
            await _classify(candidate)


async def _drain() -> None:
    global _SCAN_OWED
    while True:
        message_id, enqueued_at = await _QUEUE.get()
        started_at = time.monotonic()
        try:
            _STATE["queue_delay_ms"] = (started_at - enqueued_at) * 1000
            candidate = await run_in_db(_candidate, message_id)
            if candidate is not None:
                await _classify(candidate)
        finally:
            _STATE["service_time_ms"] = (time.monotonic() - started_at) * 1000
            _QUEUE.task_done()
        if _SCAN_OWED and _QUEUE.empty():
            _SCAN_OWED = False
            await _catch_up_scan()


def notify(message_id: str) -> None:
    """Best-effort post-admission trigger; it can never delay or fail a send."""
    global _TASK, _SCAN_OWED
    try:
        _QUEUE.put_nowait((message_id, time.monotonic()))
        if _TASK is None or _TASK.done():
            _TASK = asyncio.create_task(_drain())
    except asyncio.QueueFull:
        _SCAN_OWED = True
        dropped = _STATE.get("dropped_arrivals")
        _STATE["dropped_arrivals"] = (dropped if isinstance(dropped, int) else 0) + 1
        logger.warning("corrections classifier arrival queue is full; scan is owed")
    except Exception:
        logger.exception("corrections classifier admission notification failed")


def startup_scan() -> None:
    """Discard interrupted staging and begin catch-up without blocking startup."""
    global _TASK
    _cleanup_staging()
    if _TASK is None or _TASK.done():
        _TASK = asyncio.create_task(_catch_up_scan())


def state_snapshot() -> dict[str, object]:
    """Return the visible local-asset state without loading a model on demand."""
    return {**_STATE, "queue_depth": _QUEUE.qsize(), "scan_owed": _SCAN_OWED}


def notify_if_owner(message_id: str, *, role: str, content: str, metadata: object, duplicate: bool) -> None:
    from core.corrections_recording import is_owner_authored_message

    if not duplicate and is_owner_authored_message(role=role, content=content, metadata=metadata):
        notify(message_id)
