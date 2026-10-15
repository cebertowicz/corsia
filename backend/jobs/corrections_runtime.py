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


async def retire_fallback(worker: CorrectionsWorker, fallback_child) -> None:
    """Close the older child once the newer version has loaded."""
    await asyncio.to_thread(fallback_child.close)
    worker.children = {key: value for key, value in worker.children.items() if value is not fallback_child}


def record_version_state(worker: CorrectionsWorker, version: str, state: str, reason: str) -> None:
    """Keep a version-level startup outcome in the worker's diagnostic snapshot."""
    worker.versions = [
        {**item, "state": state, "reason": reason} if item["version"] == version else item
        for item in worker.versions
    ]


def restore_transient_fallback(
    worker: CorrectionsWorker,
    manifest,
    fallback_child,
    fallback_version: str,
    reason: str,
):
    """Hold a transiently unavailable newer version and resume its ready predecessor."""
    worker.transient_versions[manifest.version] = reason
    record_version_state(worker, manifest.version, "unavailable", reason)
    worker.child, worker.child_version = fallback_child, fallback_version
    worker.children[fallback_version] = fallback_child
    worker._set("ready", reason)
    logger.warning(
        "corrections classifier kept version {} serving while {} was unavailable: {}",
        fallback_version, manifest.version, reason,
    )
    return worker.active_manifest


async def recover_load_failure(
    worker: CorrectionsWorker,
    manifest,
    fallback_child,
    fallback_version: str | None,
    reason: str,
    failure_kind: str,
):
    """Restore a serving fallback or record a terminal deterministic startup failure."""
    if failure_kind == "deterministic":
        worker.failed_versions[manifest.version] = reason
        record_version_state(worker, manifest.version, "failed", reason)
        if fallback_child is not None:
            worker.child, worker.child_version = fallback_child, fallback_version
            worker.children[fallback_version] = fallback_child
            worker._set("ready", reason)
            logger.warning(
                "corrections classifier kept version {} serving after {} failed: {}",
                fallback_version, manifest.version, reason,
            )
            return worker.active_manifest
        worker._set("failed", reason, failure_version=manifest.version)
        return False
    return None


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
    """Record one candidate, retaining a serving older child if a newer version cannot start."""
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
            resolution = await recover_load_failure(
                worker, manifest, fallback_child, fallback_version, reason, error.kind,
            )
            if resolution is False:
                return False
            if resolution is not None:
                manifest = resolution
                fallback_child, fallback_version = None, None
                continue
            outages.append(reason)
            if len(outages) == attempts_per_kind and fallback_child is not None:
                manifest = restore_transient_fallback(
                    worker, manifest, fallback_child, str(fallback_version), reason,
                )
                fallback_child, fallback_version = None, None
                outages.clear()
            continue
        if fallback_child is not None:
            await retire_fallback(worker, fallback_child)
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
            if worker.transient_versions and worker.active_manifest is manifest:
                logger.info("corrections classifier idle restart reopens newer-version load attempts")
                worker.transient_versions.clear()
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
