"""The sole owner of the local-model setup flight and its lifecycle.

Orchestrators call named operations here; no caller receives mutable flight state.
oMLX owns its persisted download task, while the adjacent operation record keeps
its model, origin and task id so a backend restart can follow the same download.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any, Callable, Coroutine, IO

from llm import local_setup_download, local_setup_operation
from llm.local_model import LocalModel
from llm.local_profiles import format_size
from llm.local_setup_disk import require_free_disk
from llm.local_setup_results import setup_fix

STATES = frozenset({"starting", "downloading", "ready", "stopped", "setup_failed", "start_failed", "failed"})
EVENT_CATEGORY = "local_setup"
log = logging.getLogger(__name__)


@dataclass
class _Flight:
    task: asyncio.Task | None = None
    stage: str | None = None
    result: dict | None = None
    model: LocalModel | None = None
    downloaded: bool = False
    downloading: bool = False
    kind: str | None = None
    onboarding: bool = False
    server_id: str | None = None
    download_eta_seconds: int | None = None
    download_stalled: bool = False
    download_percent: int | None = None
    operation_lock: IO[str] | None = None


_flight = _Flight()


def _running() -> bool:
    return _flight.task is not None and not _flight.task.done()


def _release_operation_lock() -> None:
    local_setup_operation.unlock(_flight.operation_lock)
    _flight.operation_lock = None


def _claim_operation(kind: str, model: LocalModel | None = None, *, onboarding: bool = False) -> bool:
    """Claim the sole operation before starting work; never overwrite a persisted task."""
    lock = local_setup_operation.try_lock()
    if lock is None:
        return False
    try:
        previous = local_setup_operation.read()
        if previous is not None and (previous.result is None or previous.result.get("downloading")):
            local_setup_operation.unlock(lock)
            return False
        local_setup_operation.write(local_setup_operation.Operation.for_model(model, kind, onboarding=onboarding))
    except Exception:
        local_setup_operation.unlock(lock)
        raise
    _flight.operation_lock = lock
    _flight.kind, _flight.model, _flight.onboarding = kind, model, onboarding
    _flight.downloaded, _flight.downloading = False, False
    return True


def _record_model(model: LocalModel) -> None:
    """Persist the pick before any request to download it."""
    operation = local_setup_operation.read()
    picked = local_setup_operation.Operation.for_model(model, _flight.kind or "setup", onboarding=_flight.onboarding)
    local_setup_operation.write(replace(picked, flight_id=operation.flight_id) if operation else picked)
    _flight.model = model


def _record_download_task(model: LocalModel, task_id: str) -> None:
    operation = local_setup_operation.read()
    if operation is not None and operation.repository == model.repository:
        local_setup_operation.write(replace(operation, task_id=task_id))
    _flight.downloading = True


def start_flight(work: Coroutine[Any, Any, dict], stage: str, *,
                 operation: local_setup_operation.Operation | None = None) -> None:
    """Start claimed work, or restore the exact persisted flight for recovery."""
    if operation is not None:
        _flight.kind, _flight.model = operation.kind, operation.model
        _flight.onboarding = operation.onboarding
        _flight.downloaded = False
    _flight.result, _flight.stage = None, stage
    _flight.server_id = None
    _flight.download_eta_seconds, _flight.download_stalled, _flight.download_percent = None, False, None
    _flight.task = asyncio.create_task(work)
    _flight.task.add_done_callback(_observe_setup_task)
    _flight.task.add_done_callback(lambda _task: _notify_models_changed())


async def wait_for_operation() -> None:
    """Hold the persisted operation's advisory lock before resuming its work."""
    if _flight.operation_lock is None:
        _flight.operation_lock = await local_setup_operation.wait_for_lock()


def report_stage(stage: str) -> None:
    """Publish the current stage from setup or Settings work."""
    _flight.stage = stage
    _notify_models_changed()


def record_downloaded() -> None:
    """Record that the download completed before settings or discovery can fail."""
    _flight.downloaded, _flight.downloading = True, False


def clear_result() -> None:
    """Clear the preceding terminal result when a new request is decided."""
    _flight.result = None


def restore_terminal_flight(operation: local_setup_operation.Operation) -> None:
    """Restore the persisted outcome without starting another setup or download."""
    _flight.kind, _flight.model, _flight.onboarding = operation.kind, operation.model, operation.onboarding
    _flight.result, _flight.downloaded = operation.result, operation.downloaded
    _flight.stage, _flight.task = None, None
    _flight.downloading = False


def record_resume_failure(ref: str) -> None:
    """Publish a referenced startup delivery failure through the same durable flight."""
    operation = local_setup_operation.read()
    if _running() or (operation is not None and operation.result is not None and not operation.result.get("downloading")):
        return
    operation = operation or local_setup_operation.Operation.for_model(None, "setup", onboarding=True)
    local_setup_operation.write(operation)
    restore_terminal_flight(operation)
    finish_flight({"ok": False, "state": "failed", "cause": "other", "ref": ref,
                   "message": "CORSIA lost track of the local model download. Retry it in Settings › Providers, or save a report."})


def finish_flight(result: dict) -> dict:
    """Publish a terminal result and release ownership, keeping unfinished downloads resumable."""
    _flight.result, _flight.stage = result, None
    operation = local_setup_operation.read()
    if operation is not None:
        local_setup_operation.write(replace(operation, result=result, downloaded=_flight.downloaded))
    _flight.downloading = bool(result.get("downloading"))
    _release_operation_lock()
    return result


def _download_progress(eta_seconds: int | None, stalled: bool, percent: int | None) -> None:
    _flight.download_eta_seconds = eta_seconds
    _flight.download_stalled = stalled
    _flight.download_percent = percent


async def _download(server_id: str, report: Callable[[str], None], model: LocalModel) -> None:
    """Check disk space, then follow one persisted oMLX download without sharing flight state."""
    _flight.server_id = server_id
    _download_progress(None, False, None)
    model_dir = await local_setup_download.download_dir(server_id)
    await asyncio.to_thread(require_free_disk, model.download_size_bytes, model_dir, model.repository)
    report(f"Downloading {model.model} ({format_size(model.download_size_bytes)})")
    await _wait_for_download(server_id, report, model)


async def _wait_for_download(server_id: str, report: Callable[[str], None], model: LocalModel) -> None:
    operation = local_setup_operation.read()
    _flight.downloading = bool(operation and operation.task_id)
    await local_setup_download.wait_for_download(
        server_id, report, model, record_task=_record_download_task, on_progress=_download_progress,
        wait_seconds=local_setup_download.DOWNLOAD_WAIT_SECONDS,
        follow_seconds=local_setup_download.DOWNLOAD_FOLLOW_SECONDS,
        poll_seconds=local_setup_download.DOWNLOAD_POLL_SECONDS,
        background_poll_seconds=local_setup_download.DOWNLOAD_BACKGROUND_POLL_SECONDS,
    )


def flight_task() -> asyncio.Task | None:
    """The owned task for lifespan shutdown to cancel and join."""
    return _flight.task


def selected_model() -> LocalModel | None:
    """The model retained by the current or last flight."""
    return _flight.model


def flight_model() -> LocalModel | None:
    """The running flight's model, if picked."""
    return _flight.model if _running() else None


def setup_stage() -> str | None:
    """The current stage while work is running."""
    return (_flight.stage or "Setting up local model") if _running() else None


def setup_kind() -> str | None:
    """The wire kind: setup or settings."""
    return _flight.kind


def setup_failure() -> dict | None:
    """The last failed result, if any."""
    result = _flight.result
    return result if result and not result.get("ok") else None


def download_estimate() -> tuple[int | None, bool]:
    """The current ETA and stalled flag."""
    return _flight.download_eta_seconds, _flight.download_stalled


def download_percent() -> int | None:
    """The structured progress reported by the current oMLX download task."""
    return _flight.download_percent


def terminal_outcome(result: dict | None, *, downloaded: bool = False) -> str:
    """Derive the one setup outcome, or validate an already projected wire state."""
    if result is None or result.get("cancelled"):
        return "stopped"
    if "state" in result:
        return result["state"] if result["state"] in STATES else "stopped"
    if result.get("downloading"):
        return "downloading"
    if result.get("ok"):
        return "ready"
    if result.get("pre_download"):
        return "setup_failed"
    return "start_failed" if downloaded else "failed"


def setup_status() -> dict[str, Any]:
    """Bounded live state for setup and the first-run download rail."""
    result, model = _flight.result, _flight.model
    stage = setup_stage()
    state = ("downloading" if _flight.downloading else "starting") if _running() else terminal_outcome(
        result, downloaded=_flight.downloaded,
    )
    failed = result or {}
    return {
        "state": state, "model": model.repository if model else None,
        "percent": _flight.download_percent,
        "eta_seconds": _flight.download_eta_seconds,
        "download_size": model.download_size_bytes if model else None,
        "stalled": _flight.download_stalled,
        "cause": result.get("cause") if result else None,
        "message": result.get("message") if result else None,
        "ref": result.get("ref") if result else None,
        "bytes_to_free": result.get("bytes_to_free") if result else None,
        "fix": setup_fix(failed.get("cause"), failed.get("bytes_to_free"))
        if state in {"failed", "setup_failed", "start_failed"} else None,
    }


def _emit(action: str, **properties: object) -> None:
    from infra.product_events import emit_product_event

    emit_product_event(EVENT_CATEGORY, action, properties=dict(properties))


def _in_progress_result(*, notice: str | None = None) -> dict:
    stage = setup_stage()
    result = {"ok": True, "in_progress": True, "stage": stage, "progress": [stage]}
    if notice:
        result["notice"] = notice
    return result


def _skipped(reason: str, message: str) -> dict:
    _emit("skipped", reason=reason)
    return {"ok": True, "skipped": reason, "message": message, "progress": []}


def _observe_setup_task(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("Local setup task ended with an error", exc_info=task.exception())


def _notify_models_changed() -> None:
    from infra.state.ws import notify_models_changed

    notify_models_changed()


async def _notify_terminal_setup_download(result: dict) -> None:
    """Wake onboarding only for a flight explicitly begun by onboarding."""
    if not _flight.onboarding or _flight.kind != "setup" or result.get("downloading"):
        return
    if terminal_outcome(result, downloaded=_flight.downloaded) == "stopped":
        return
    try:
        from onboarding.local_download_wake import flush_pending_local_download

        await flush_pending_local_download()
    except Exception:
        log.exception("Couldn't notify first-run chat of terminal local setup download")


def _stopped_following(model: LocalModel | None, events: list[str]) -> dict:
    _emit("downloading_stopped", model=model.repository if model else None)
    name = model.display_name if model else "The local model"
    return {"ok": True, "downloading": True,
            "message": f"{name} is still downloading in oMLX; CORSIA stopped following it.", "progress": events}


async def cancel_local_setup() -> dict:
    """Cancel exactly the owned download task and publish cancellation once."""
    from llm.providers.omlx_endpoints import admin_request

    task = _flight.task
    if task is None or task.done():
        return {"ok": True, "cancelled": False}
    operation = local_setup_operation.read()
    task_id = operation.task_id if operation else None
    if task_id and _flight.server_id:
        response = await admin_request(
            {"server_id": _flight.server_id}, "POST", f"/hf/cancel/{task_id}", timeout=15.0,
        )
        if response.status_code not in (200, 202, 204, 404, 409):
            raise RuntimeError(f"oMLX could not cancel the model download (HTTP {response.status_code}).")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finish_flight({"ok": True, "cancelled": True})
    _download_progress(None, False, None)
    _notify_models_changed()
    _emit("cancelled", model=operation.repository if operation else None)
    return {"ok": True, "cancelled": True}
