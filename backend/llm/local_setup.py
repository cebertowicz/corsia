"""Canonical local-model setup: one flight that installs the local model oMLX picks.

The model is the managed oMLX recommender's pick (``local_model``), taken while
Welcome is open (``local_setup_prepare``) or by the flight itself; nothing
downloads without one. One explicit setup action makes at most one new oMLX
HF-download request. oMLX
owns the persisted download task. The backend server is the only owner of a
setup flight: one in-process record holds the running task, its stage and the
last terminal result, so a server restart ends the flight and clears the result.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, IO

from llm.local_model import LocalModel
from llm.local_setup_gates import host_ram_bytes, local_memory_ok  # noqa: F401  (patched through this module)
from llm.local_setup_gates import limited_subscription_message as _limited_subscription_message
from llm.local_setup_gates import subscription_available as _subscription_available
from llm.local_setup_results import failure_message as _failure_message
from llm.local_setup_results import SETUP_COMMAND, local_setup_error
# Re-exported: the flight modules and their tests reach these through this module.
from llm.local_setup_auth import (  # noqa: F401
    SKIP_AUTH_SETTING,
    UPDATE_OMLX_MESSAGE,
    _omlx_supports_loopback_auth,
    omlx_version,
    registered_local_key,
    secure_local_omlx,
)
from llm.local_setup_ownership import ensure_omlx as _ensure_omlx
from llm.local_setup_ownership import require_free_disk
from llm.local_setup_ownership import run_command as _run
from llm.local_profiles import format_size
from llm import local_setup_cancel, local_setup_download, local_setup_operation
from llm.local_setup_download import (
    DOWNLOAD_BACKGROUND_POLL_SECONDS,
    DOWNLOAD_FOLLOW_SECONDS,
    DOWNLOAD_POLL_SECONDS,
    DOWNLOAD_WAIT_SECONDS,
    StillDownloading,
)
from llm.local_setup_runtime import (
    HEALTH_WAIT_SECONDS,
    LOCAL_HOST,
    LOCAL_SERVER_NAME,
    configure_global as _configure_global,
    configure_model_settings as _configure_model_settings,
    omlx_answers as _omlx_answers,
)

EVENT_CATEGORY = "local_setup"

log = logging.getLogger(__name__)


async def _wait_for_download(
    server_id: str, progress: Callable[[str], None], model: LocalModel,
) -> None:
    """Follow oMLX's persisted download task using this flight's live state."""
    await local_setup_download.wait_for_download(
        server_id, progress, model, record_task=_record_download_task, flight=_flight,
        wait_seconds=DOWNLOAD_WAIT_SECONDS, poll_seconds=DOWNLOAD_POLL_SECONDS,
        background_poll_seconds=DOWNLOAD_BACKGROUND_POLL_SECONDS,
        follow_seconds=DOWNLOAD_FOLLOW_SECONDS,
    )


async def _download_dir(server_id: str):
    return await local_setup_download.download_dir(server_id)


async def _download(server_id: str, report: Callable[[str], None], model: LocalModel) -> None:
    """Check the disk oMLX downloads to, then request and follow the model's download."""
    _flight.server_id = server_id
    _flight.download_eta_seconds = None
    _flight.download_stalled = False
    model_dir = await _download_dir(server_id)
    await asyncio.to_thread(require_free_disk, model.download_size_bytes, model_dir, model.repository)
    report(f"Downloading {model.model} ({format_size(model.download_size_bytes)})")
    await _wait_for_download(server_id, report, model)


@dataclass
class _Flight:
    task: asyncio.Task | None = None
    stage: str | None = None
    result: dict | None = None
    # The model this flight downloads, once it is picked or resumed.
    model: LocalModel | None = None
    following: bool = False
    kind: str | None = None
    server_id: str | None = None
    download_eta_seconds: int | None = None
    download_stalled: bool = False
    operation_lock: IO[str] | None = None


_flight = _Flight()


def _running() -> bool:
    return _flight.task is not None and not _flight.task.done()


def _release_operation_lock() -> None:
    local_setup_operation.unlock(_flight.operation_lock)
    _flight.operation_lock = None


def _claim_operation(kind: str, model: LocalModel | None = None) -> bool:
    """Exclusively own one flight; persist its model now when it is already known.

    A flight that still has to pick persists its model once it has one
    (``_record_model``), before it can request a download.
    """
    lock = local_setup_operation.try_lock()
    if lock is None:
        return False
    try:
        if local_setup_operation.read() is not None:
            local_setup_operation.unlock(lock)
            return False
        if model is not None:
            local_setup_operation.write(local_setup_operation.Operation.for_model(model, kind))
    except Exception:
        local_setup_operation.unlock(lock)
        raise
    _flight.operation_lock = lock
    _flight.kind = kind
    _flight.model = model
    return True


def _record_model(model: LocalModel) -> None:
    """Persist the picked model so a restarted backend resumes this flight's download."""
    local_setup_operation.write(local_setup_operation.Operation.for_model(model, _flight.kind or "setup"))
    _flight.model = model


def _record_download_task(model: LocalModel, task_id: str) -> None:
    operation = local_setup_operation.read()
    if operation is None or operation.repository != model.repository:
        return
    local_setup_operation.write(local_setup_operation.Operation.for_model(model, operation.kind, task_id))


def flight_model() -> LocalModel | None:
    """The model the running flight downloads, else None."""
    return _flight.model if _running() else None


def setup_stage() -> str | None:
    """The running flight's current stage, else None."""
    return (_flight.stage or "Setting up local model") if _running() else None


def setup_kind() -> str | None:
    """Which flight the record is, else None.

    "starter" is the app's add-the-default-model download into the person's own oMLX
    (``begin_add_starter_model``); "setup" is every other flight. Both are wire values.
    """
    return _flight.kind


def setup_failure() -> dict | None:
    """The last flight's result when it failed, else None."""
    result = _flight.result
    return result if result and not result.get("ok") else None


def setup_stopped_following() -> dict | None:
    """The last flight's result when CORSIA stopped following a download oMLX still runs."""
    result = _flight.result
    return result if result and result.get("downloading") else None


def download_estimate() -> tuple[int | None, bool]:
    """The live download ETA, or its stalled state, from the one setup flight."""
    return _flight.download_eta_seconds, _flight.download_stalled


async def cancel_local_setup() -> dict:
    return await local_setup_cancel.cancel_setup(_flight, notify=_notify_models_changed, emit=_emit)


def setup_status() -> dict[str, Any]:
    """Bounded live state for setup and first-run download presentation."""
    result = _flight.result
    model = _flight.model
    stage = setup_stage() if _running() else None
    if _running():
        state = "downloading" if _flight.following else "running"
    elif result is None:
        state = "idle"
    elif result.get("downloading"):
        state = "downloading"
    else:
        # Success follows a completed catalog refresh, so this model is usable.
        state = "ready" if result.get("ok") else "failed"
    percent = None
    if stage:
        import re

        match = re.search(r"(\d{1,3})%", stage)
        percent = min(100, int(match.group(1))) if match else None
    return {
        "state": state,
        "model": model.view() if model else None,
        "percent": percent,
        "eta_seconds": _flight.download_eta_seconds,
        "download_size": format_size(model.download_size_bytes) if model else None,
        "stalled": _flight.download_stalled,
        "cause": result.get("cause") if result else None,
        "ref": result.get("ref") if result else None,
        "bytes_to_free": result.get("bytes_to_free") if result else None,
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
    """Retrieve a finished flight's exception so none goes unobserved."""
    if not task.cancelled() and task.exception() is not None:
        log.error("Local setup task ended with an error", exc_info=task.exception())


def _notify_models_changed() -> None:
    from infra.state.ws import notify_models_changed

    notify_models_changed()


async def _notify_terminal_setup_download(result: dict) -> None:
    """Wake first-run chat for terminal setup flights; Settings downloads never wake it."""
    if _flight.kind != "setup":
        return
    model = _flight.model
    if result.get("downloading"):
        outcome = "not_loaded"
    elif result.get("ok"):
        outcome = "ready"
    else:
        outcome = "start_failed" if model is None else "failed"
    flight_id = (local_setup_operation.read() or local_setup_operation.Operation.for_model(
        model or LocalModel("local-model", 0, 0), "setup",
    )).task_id or (model.repository if model else "local-model")
    try:
        from onboarding.local_download_wake import notify_terminal_setup_download

        await notify_terminal_setup_download(
            flight_id, outcome, model=model.repository if model else None,
            cause=result.get("cause"), ref=result.get("ref"),
            bytes_to_free=result.get("bytes_to_free"),
        )
    except Exception:
        log.exception("Couldn't notify first-run chat of terminal local setup download")


async def _run_flight(*, model: LocalModel | None = None,
                      on_stage: Callable[[str], None] | None = None) -> dict:
    """Run the one setup flight against CORSIA's managed oMLX process."""
    events: list[str] = []

    def report(stage: str) -> None:
        _flight.stage = stage
        _notify_models_changed()
        events.append(stage)
        if on_stage is not None:
            on_stage(stage)

    _flight.following = False
    try:
        result = await _setup_new_omlx(report, events, model)
    except asyncio.CancelledError:
        _release_operation_lock()
        raise
    except StillDownloading:
        result = {**_stopped_following(_flight.model, events), "next_action": f"Run `{SETUP_COMMAND}` to follow it again."}
    except Exception as exc:
        log.exception("Local setup failed")
        result = {**local_setup_error(_failure_message(exc)), "progress": events,
                  "cause": type(exc).__name__}
        _emit("failed", error_type=type(exc).__name__)
    await _notify_terminal_setup_download(result)
    return local_setup_operation.finish(_flight, result)


def _stopped_following(model: LocalModel | None, events: list[str]) -> dict:
    """The result when the follow window ended while oMLX keeps downloading (setup and Settings)."""
    _emit("downloading_stopped", model=model.repository if model else None)
    name = model.display_name if model else "The local model"
    return {
        "ok": True, "downloading": True,
        "message": f"{name} is still downloading in oMLX; CORSIA stopped following it.",
        "progress": events,
    }


def flight_task() -> asyncio.Task | None:
    """The flight's task for lifespan shutdown to cancel and join, else None."""
    return _flight.task


async def resume_pending_setup() -> bool:
    """Re-follow a persisted oMLX task after a backend restart, without posting a duplicate."""
    if _running():
        return True
    operation = local_setup_operation.read()
    if operation is None:
        return False
    if operation.kind not in {"setup", "starter", "settings"}:
        log.warning("discarding unreadable local setup recovery record")
        local_setup_operation.clear()
        return False
    if not await asyncio.to_thread(local_memory_ok):
        log.warning("discarding local setup recovery record: this Mac has too little memory for a local model")
        local_setup_operation.clear()
        return False
    model = operation.model

    _flight.result = None
    _flight.model = model
    _flight.kind = operation.kind
    _flight.stage = "Waiting to resume local model setup"

    async def resume() -> dict:
        if operation.kind == "settings":
            from llm.local_settings_download import resume as resume_settings_download

            return await resume_settings_download(model)
        await local_setup_operation.wait_for_lock(_flight)
        return await _run_flight(model=model)

    _flight.task = asyncio.create_task(resume())
    _flight.task.add_done_callback(_observe_setup_task)
    _flight.task.add_done_callback(lambda _task: _notify_models_changed())
    _emit("resumed", model=model.repository, kind=operation.kind)
    return True


async def begin_local_setup(
    *,
    no_subscription_only: bool = False,
    automatic: bool = False,
) -> dict:
    """Start the one setup flight without blocking the caller, or say why not.

    Memory and the subscription are each decided once, here; the model is picked
    by the flight (or was, by prepare). A request made while a flight runs reports
    that flight; any other request clears the previous terminal result before it is decided.
    """
    if _running():
        return _in_progress_result()
    if local_setup_operation.read() is not None:
        if await resume_pending_setup():
            return _in_progress_result()
        return local_setup_error("CORSIA could not resume the local model setup safely.")
    memory_ok = await asyncio.to_thread(local_memory_ok)
    _flight.result = None
    subscription = await _subscription_available() if no_subscription_only else None
    if subscription is not None:
        source = "Claude Code" if subscription.provider == "claude-code" else "Codex"
        account = f" as {subscription.label}" if subscription.label else ""
        return _skipped(
            "subscription_available",
            f"{source} is signed in{account}, so the local model is skipped. You can add one from the app.",
        )
    notice = await _limited_subscription_message() if no_subscription_only else None
    if not memory_ok:
        if notice:
            message = "This computer can't run a local model well. Connect another subscription or an API key in the app."
            return _skipped("ineligible", f"{notice}\n{message}")
        message = "This computer can't run a local model well. Connect a subscription or an API key in the app."
        if automatic:
            return _skipped("ineligible", message)
        _emit("skipped", reason="ineligible")
        return {**local_setup_error(message), "progress": []}
    if _running():
        return _in_progress_result()
    if not _claim_operation("setup"):
        await resume_pending_setup()
        return _in_progress_result()
    _flight.stage = "Installing"
    _flight.task = asyncio.create_task(_run_flight())
    _flight.task.add_done_callback(_observe_setup_task)
    _flight.task.add_done_callback(lambda _task: _notify_models_changed())
    _emit("started")
    return _in_progress_result(notice=notice)



from llm.local_settings_download import begin as begin_settings_download  # noqa: E402
from llm.local_setup_new_omlx import _setup_new_omlx  # noqa: E402  (reads this module's names at call time)
