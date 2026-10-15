"""One-child classifier lifecycle for the worker-owned corrections state machine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from loguru import logger

from core.corrections_child import ClassifierUnavailable, MessageFailed
from core.corrections_recording import CorrectionCandidate

if TYPE_CHECKING:
    from core.corrections_classifier import AssetManifest
    from jobs.corrections_realtime import CorrectionsWorker


OutcomeKind = Literal["recorded", "unreadable", "outage", "version_failed"]


@dataclass(frozen=True)
class ClassificationOutcome:
    """The explicit result of one fixed-manifest classification attempt."""

    kind: OutcomeKind
    reason: str | None = None
    failure_version: str | None = None


async def drop_child(worker: CorrectionsWorker) -> None:
    """Close the sole child and clear its matching manifest."""
    child = worker.child
    if child is None:
        return
    await asyncio.to_thread(child.close)
    if worker.child is child:
        worker.child = None
        worker.child_manifest = None


def record_version_state(worker: CorrectionsWorker, version: str, state: str, reason: str | None) -> None:
    """Keep one candidate's diagnostic state in the worker snapshot."""
    worker.versions = [
        {**item, "state": state, "reason": reason} if item["version"] == version else item
        for item in worker.versions
    ]


async def _child_for(worker: CorrectionsWorker, manifest: AssetManifest, child_factory):
    """Return the sole child for this manifest, closing a different version first."""
    if worker.child is not None and worker.child_manifest.version != manifest.version:
        await drop_child(worker)
    if worker.child is None:
        worker.child = child_factory(manifest)
        worker.child_manifest = manifest
    return worker.child


async def classify(
    worker: CorrectionsWorker,
    candidate: CorrectionCandidate,
    manifest: AssetManifest,
    *,
    attempts_per_kind: int,
    has_fallback: bool,
    child_factory,
    clock,
    record_classification,
    record_unreadable,
) -> ClassificationOutcome:
    """Classify using only ``manifest``; selection and version retry belong to the caller."""
    outages: list[str] = []
    failures: list[str] = []
    consecutive_load_exits = 0
    worker.call_seconds = 0.0
    while len(outages) < attempts_per_kind and len(failures) < attempts_per_kind:
        child = await _child_for(worker, manifest, child_factory)
        if worker.state != "ready" and not (child.loaded and child.running):
            worker._set("loading", "starting the model and its self-test")
        try:
            await asyncio.to_thread(child.ensure_loaded)
        except ClassifierUnavailable as error:
            reason = str(error)
            logger.warning("corrections classifier did not load {}: {}", manifest.version, error)
            await drop_child(worker)
            if error.kind == "transient" and error.cause == "child_exited":
                consecutive_load_exits += 1
                if consecutive_load_exits == 2 and has_fallback:
                    error = ClassifierUnavailable(reason, kind="deterministic", cause=error.cause)
            if error.kind == "deterministic":
                worker.failed_versions[manifest.version] = reason
                record_version_state(worker, manifest.version, "failed", reason)
                return ClassificationOutcome("version_failed", reason, manifest.version)
            outages.append(reason)
            continue
        consecutive_load_exits = 0
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
        worker.version = manifest.classifier_version()
        worker._set("ready", None)
        return ClassificationOutcome("recorded")
    if len(failures) == attempts_per_kind:
        await record_unreadable(candidate, manifest.classifier_version(), failures[-1])
        worker.version = manifest.classifier_version()
        worker._set("ready", None)
        return ClassificationOutcome("unreadable")
    reason = outages[-1]
    worker._set("unavailable", reason)
    return ClassificationOutcome("outage", reason)
