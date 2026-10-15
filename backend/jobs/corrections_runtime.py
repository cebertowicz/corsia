"""Child lifecycle transitions for the worker-owned corrections classifier state machine.

This module stores no state: ``CorrectionsWorker`` remains the sole owner of its child,
selected versions, and state snapshot. Keeping the transition mechanics here leaves the
worker module small enough to retain its complete state-machine contract.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from core.corrections_child import ClassifierUnavailable, MessageFailed
from core.corrections_recording import CorrectionCandidate

if TYPE_CHECKING:
    from jobs.corrections_realtime import CorrectionsWorker


async def drop_child(worker: CorrectionsWorker) -> None:
    """Stop the current child; forget it only after exit, so shutdown still sees it."""
    child = worker.child
    if child is None:
        return
    await asyncio.to_thread(child.close)
    if worker.child is child:
        worker.child = None
        worker.child_version = None
    worker.children = {key: value for key, value in worker.children.items() if value is not child}


async def classify(
    worker: CorrectionsWorker,
    candidate: CorrectionCandidate,
    *,
    attempts_per_kind: int,
    child_factory,
    clock,
    record_classification,
    record_unreadable,
) -> bool:
    """Record one candidate, retaining the serving child if a newer version cannot start.

    ``False`` leaves the candidate pending. A deterministic newer-version failure is
    recorded once on the worker and never retried until a different pinned version wins.
    """
    manifest = await worker._verified_manifest()
    if manifest is None:
        return False
    outages: list[str] = []
    failures: list[str] = []
    worker.call_seconds = 0.0
    fallback_child = worker.child if worker.child_version != manifest.version else None
    fallback_version = worker.child_version if fallback_child is not None else None
    if fallback_child is not None:
        worker.child = child_factory(manifest)
        worker.child_version = manifest.version
        worker.children[manifest.version] = worker.child
    while len(outages) < attempts_per_kind and len(failures) < attempts_per_kind:
        if worker.child is None:
            worker.child = child_factory(manifest)
            worker.child_version = manifest.version
            worker.children[manifest.version] = worker.child
        child = worker.child
        if worker.state != "ready" and not (child.loaded and child.running):
            worker._set("loading", "starting the model and its self-test")
        try:
            await asyncio.to_thread(child.ensure_loaded)
        except ClassifierUnavailable as error:
            reason = str(error)
            logger.warning("corrections classifier did not load: {}", error)
            await drop_child(worker)
            if reason.startswith("load error:"):
                worker._mark_version_failed(manifest.version, reason)
                if fallback_child is not None:
                    worker.child, worker.child_version = fallback_child, fallback_version
                    worker.children[fallback_version] = fallback_child
                    logger.warning(
                        "corrections classifier kept version {} serving after {} failed: {}",
                        fallback_version, manifest.version, reason,
                    )
                    return False
                worker._set("failed", reason)
                return False
            if fallback_child is not None:
                worker.child, worker.child_version = fallback_child, fallback_version
                worker.children[fallback_version] = fallback_child
                worker._set("ready", reason)
                logger.warning(
                    "corrections classifier kept version {} serving while {} was unavailable: {}",
                    fallback_version, manifest.version, reason,
                )
                return False
            outages.append(reason)
            continue
        if fallback_child is not None:
            await asyncio.to_thread(fallback_child.close)
            worker.children = {key: value for key, value in worker.children.items() if value is not fallback_child}
            fallback_child = None
        worker.active_manifest = manifest
        worker.version = manifest.classifier_version()
        call_started = clock()
        try:
            result = await asyncio.to_thread(child.classify, list(candidate.context), candidate.content)
        except MessageFailed as error:
            failures.append(error.reason)
            logger.warning("corrections classifier failed on one input: {}", error.reason)
            await drop_child(worker)
            continue
        except ClassifierUnavailable as error:
            outages.append(str(error))
            await drop_child(worker)
            continue
        finally:
            worker.call_seconds += clock() - call_started
        await record_classification(candidate, result)
        worker._set("ready", None)
        return True
    if len(failures) == attempts_per_kind:
        await record_unreadable(candidate, manifest.classifier_version(), failures[-1])
        return True
    worker._set("unavailable", outages[-1])
    return False
