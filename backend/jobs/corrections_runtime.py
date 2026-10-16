"""One generic ONNX child lifecycle for the worker-owned corrections state machine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from loguru import logger

from core.corrections_classifier import CORRECTIONS_SPEC, classification_from_reply
from core.corrections_recording import ClassifierVersion, CorrectionCandidate
from core.onnx_asset import AssetManifest
from core.onnx_child import OnnxChild, OnnxMessageFailed, OnnxUnavailable

if TYPE_CHECKING:
    from jobs.corrections_realtime import CorrectionsWorker


OutcomeKind = Literal["recorded", "unreadable", "outage", "version_failed"]


@dataclass(frozen=True)
class ClassificationOutcome:
    """The explicit result of one fixed-manifest classification attempt."""

    kind: OutcomeKind
    reason: str | None = None
    failure_version: str | None = None
    only_version_load_crash: bool = False
    load_succeeded: bool = False


async def drop_child(worker: CorrectionsWorker) -> bool:
    """Close the sole child, retaining an uncontrollable one rather than starting another."""
    child = worker.child
    if child is None:
        return True
    try:
        await asyncio.to_thread(child.close)
    except OnnxUnavailable as error:
        logger.warning("corrections classifier child did not close: {}", error)
        return False
    if worker.child is child:
        worker.child = None
        worker.child_manifest = None
    return True


def record_version_state(worker: CorrectionsWorker, version: str, state: str, reason: str | None) -> None:
    """Keep one candidate's diagnostic state in the worker snapshot."""
    worker.versions = [
        {**item, "state": state, "reason": reason} if item["version"] == version else item
        for item in worker.versions
    ]


async def _child_for(worker: CorrectionsWorker, manifest: AssetManifest) -> OnnxChild:
    """Return the sole generic child for this manifest, closing a different version first."""
    if worker.child is not None and worker.child_manifest.version != manifest.version:
        if not await drop_child(worker):
            raise OnnxUnavailable("child_did_not_exit")
    if worker.child is None:
        worker.child = OnnxChild(manifest, CORRECTIONS_SPEC)
        worker.child_manifest = manifest
    return worker.child


def _version_for(manifest: AssetManifest) -> ClassifierVersion:
    """Build the existing corrections identity from generic manifest version data."""
    return ClassifierVersion(**manifest.head_version(CORRECTIONS_SPEC.required_heads[0]))


async def classify(
    worker: CorrectionsWorker,
    candidate: CorrectionCandidate,
    manifest: AssetManifest,
    *,
    attempts_per_kind: int,
    has_fallback: bool,
    clock,
    record_classification,
    record_unreadable,
) -> ClassificationOutcome:
    """Classify using only ``manifest``; selection and version retry belong to the caller."""
    outages: list[str] = []
    failures: list[str] = []
    consecutive_load_exits = 0
    load_succeeded = False
    worker.call_seconds = 0.0
    while len(outages) < attempts_per_kind and len(failures) < attempts_per_kind:
        try:
            child = await _child_for(worker, manifest)
            if worker.state != "ready" and not (child.loaded and child.running):
                worker._set("loading", "starting the model and its self-test")
            await asyncio.to_thread(child.ensure_loaded)
        except OnnxUnavailable as error:
            reason = str(error)
            logger.warning("corrections classifier did not load {}: {}", manifest.version, error)
            await drop_child(worker)
            if error.kind == "transient" and error.cause == "child_exited":
                consecutive_load_exits += 1
                if consecutive_load_exits == 2 and has_fallback:
                    error = OnnxUnavailable(reason, kind="deterministic", cause=error.cause)
                elif consecutive_load_exits == 2:
                    return ClassificationOutcome("outage", reason, only_version_load_crash=True)
            if error.kind == "deterministic":
                worker.failed_versions[manifest.version] = reason
                record_version_state(worker, manifest.version, "failed", reason)
                return ClassificationOutcome("version_failed", reason, manifest.version, load_succeeded=load_succeeded)
            outages.append(reason)
            continue
        consecutive_load_exits = 0
        load_succeeded = True
        call_started = clock()
        try:
            reply = await asyncio.to_thread(child.score, {
                "context": list(candidate.context),
                "owner": candidate.content,
                "head": CORRECTIONS_SPEC.required_heads[0],
            })
            result = classification_from_reply(reply)
        except OnnxMessageFailed as error:
            failures.append(error.reason)
            logger.warning("corrections classifier failed on one input: {}", error.reason)
            await drop_child(worker)
            continue
        except OnnxUnavailable as error:
            outages.append(str(error))
            await drop_child(worker)
            continue
        finally:
            worker.call_seconds += clock() - call_started
        await record_classification(candidate, result)
        worker.version = result.version
        worker._set("ready", None)
        return ClassificationOutcome("recorded", load_succeeded=load_succeeded)
    if len(failures) == attempts_per_kind:
        version = _version_for(manifest)
        await record_unreadable(candidate, version, failures[-1])
        worker.version = version
        worker._set("ready", None)
        return ClassificationOutcome("unreadable", load_succeeded=load_succeeded)
    reason = outages[-1]
    worker._set("unavailable", reason)
    return ClassificationOutcome("outage", reason, load_succeeded=load_succeeded)
