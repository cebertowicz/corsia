"""Canonical local-model setup: one flight that installs a named local profile.

One explicit setup action makes at most one new oMLX HF-download request. oMLX
owns the persisted download task. The backend server is the only owner of a
setup flight: one in-process record holds the running task, its stage and the
last terminal result, so a server restart ends the flight and clears the result.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, IO

from llm.local_setup_gates import Eligibility, host_ram_bytes, profile_eligibility, resolve_profile
from llm.local_setup_gates import limited_subscription_message as _limited_subscription_message
from llm.local_setup_gates import subscription_available as _subscription_available
from llm.local_setup_results import failure_message as _failure_message
from llm.local_setup_results import local_setup_error, setup_command
# Re-exported: the flight modules and their tests reach these through this module.
from llm.local_setup_auth import (  # noqa: F401
    SKIP_AUTH_SETTING,
    UPDATE_OMLX_MESSAGE,
    _omlx_supports_loopback_auth,
    foreign_omlx_failure,
    omlx_version,
    registered_local_key,
    secure_local_omlx,
)
from llm.local_setup_ownership import ensure_omlx as _ensure_omlx
from llm.local_setup_ownership import existing_install_is_foreign
from llm.local_setup_ownership import omlx_log_detail, own_omlx_admin_key, own_omlx_port, require_free_disk
from llm.local_setup_ownership import run_command as _run
from llm.local_profiles import DEFAULT_PROFILE, LocalProfile, format_size, profile_by_id
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
    register_loopback_server as _register_loopback_server,
    wait_for_health as _wait_for_health,
)

EVENT_CATEGORY = "local_setup"

log = logging.getLogger(__name__)


async def _wait_for_download(
    server_id: str, progress: Callable[[str], None], profile: LocalProfile,
) -> None:
    """Follow oMLX's persisted download task using this flight's live state."""
    await local_setup_download.wait_for_download(
        server_id, progress, profile, record_task=_record_download_task, flight=_flight,
        wait_seconds=DOWNLOAD_WAIT_SECONDS, poll_seconds=DOWNLOAD_POLL_SECONDS,
        background_poll_seconds=DOWNLOAD_BACKGROUND_POLL_SECONDS,
        follow_seconds=DOWNLOAD_FOLLOW_SECONDS,
    )


async def _download_dir(server_id: str):
    return await local_setup_download.download_dir(server_id)


async def _download(server_id: str, report: Callable[[str], None], profile: LocalProfile) -> None:
    """Check the disk oMLX downloads to, then request and follow the profile's download."""
    _flight.server_id = server_id
    _flight.download_eta_seconds = None
    _flight.download_stalled = False
    model_dir = await _download_dir(server_id)
    await asyncio.to_thread(require_free_disk, profile.download_size_bytes, model_dir, profile.repository)
    report(f"Downloading {profile.model} ({format_size(profile.download_size_bytes)})")
    await _wait_for_download(server_id, report, profile)


@dataclass
class _Flight:
    task: asyncio.Task | None = None
    stage: str | None = None
    result: dict | None = None
    eligibility: Eligibility | None = None
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


def _claim_operation(profile: LocalProfile, kind: str) -> bool:
    """Persist and exclusively own one flight before it can request a download."""
    lock = local_setup_operation.try_lock()
    if lock is None:
        return False
    try:
        if local_setup_operation.read() is not None:
            local_setup_operation.unlock(lock)
            return False
        local_setup_operation.write(local_setup_operation.Operation(profile.id, kind))
    except Exception:
        local_setup_operation.unlock(lock)
        raise
    _flight.operation_lock = lock
    return True


def _record_download_task(profile: LocalProfile, task_id: str) -> None:
    operation = local_setup_operation.read()
    if operation is None or operation.profile_id != profile.id:
        return
    local_setup_operation.write(
        local_setup_operation.Operation(profile.id, operation.kind, task_id),
    )


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
    """The flight record exactly as it stands: no inference from other state.

    ``downloading`` is a wait that outlived the foreground budget while oMLX
    keeps downloading; it is not a failure.
    """
    if _running():
        state = "downloading" if _flight.following else "running"
        return {"state": state, "stage": setup_stage(), "result": None}
    if _flight.result is None:
        return {"state": "idle", "stage": None, "result": None}
    if _flight.result.get("downloading"):
        return {"state": "downloading", "stage": None, "result": _flight.result}
    state = "succeeded" if _flight.result.get("ok") else "failed"
    return {"state": state, "stage": None, "result": _flight.result}


def _emit(action: str, **properties: object) -> None:
    from infra.product_events import emit_product_event

    emit_product_event(EVENT_CATEGORY, action, properties=dict(properties))


def _in_progress_result(eligibility: Eligibility | None, *, notice: str | None = None) -> dict:
    stage = setup_stage()
    result = {
        "ok": True,
        "in_progress": True,
        "stage": stage,
        "eligibility": eligibility.__dict__ if eligibility else None,
        "progress": [stage],
    }
    if notice:
        result["notice"] = notice
    return result


def _skipped(reason: str, message: str, eligibility: Eligibility) -> dict:
    _emit("skipped", reason=reason)
    return {
        "ok": True, "skipped": reason, "message": message,
        "eligibility": eligibility.__dict__, "progress": [],
    }


def _observe_setup_task(task: asyncio.Task) -> None:
    """Retrieve a finished flight's exception so none goes unobserved."""
    if not task.cancelled() and task.exception() is not None:
        log.error("Local setup task ended with an error", exc_info=task.exception())


def _notify_models_changed() -> None:
    from infra.state.ws import notify_models_changed

    notify_models_changed()


async def _run_flight(eligibility: Eligibility, *, profile: LocalProfile, into_own_omlx: bool = False,
                      on_stage: Callable[[str], None] | None = None) -> dict:
    """Run the one setup flight for ``profile`` and record its terminal result.

    Every setup entry path leaves an oMLX it did not install untouched and uses it.
    ``into_own_omlx`` downloads the profile into that oMLX and writes nothing to it;
    ``on_stage`` also receives each stage (the installer prints them).
    """
    events: list[str] = []

    def report(stage: str) -> None:
        _flight.stage = stage
        _notify_models_changed()
        events.append(stage)
        if on_stage is not None:
            on_stage(stage)

    _flight.following = False
    try:
        if into_own_omlx:
            result = await download_into_own_omlx(report, events, profile)
        elif await asyncio.to_thread(existing_install_is_foreign):
            failure = await asyncio.to_thread(foreign_omlx_failure)
            if failure:
                raise RuntimeError(failure)
            result = await adopt_existing_omlx(report, eligibility, events)
        else:
            result = await _setup_new_omlx(report, eligibility, events, profile)
    except asyncio.CancelledError:
        _release_operation_lock()
        raise
    except StillDownloading:
        _emit("downloading_stopped", profile=profile.id)
        result = {
            "ok": True, "downloading": True,
            "message": f"{profile.display_name} is still downloading in oMLX; CORSIA stopped following it.",
            "next_action": f"Run `{setup_command(profile.id)}` to follow it again.",
            "eligibility": eligibility.__dict__, "progress": events,
        }
    except Exception as exc:
        log.exception("Local setup failed for %s", profile.id)
        result = {
            **local_setup_error(_failure_message(exc), profile.id),
            "eligibility": eligibility.__dict__, "progress": events,
        }
        _emit("failed", error_type=type(exc).__name__, profile=profile.id)
    _flight.result = result
    _flight.stage = None
    _flight.following = False
    if result.get("ok") and not result.get("downloading"):
        local_setup_operation.clear()
    elif not result.get("downloading"):
        # A terminal oMLX error cannot be followed. Removing its stale task id
        # lets an explicit retry create one new task instead of replaying it.
        local_setup_operation.clear()
    _release_operation_lock()
    return result


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
    profile = profile_by_id(operation.profile_id)
    if profile is None or operation.kind not in {"setup", "starter"}:
        log.warning("discarding unreadable local setup recovery record")
        local_setup_operation.clear()
        return False
    eligibility = profile_eligibility(profile, await asyncio.to_thread(host_ram_bytes))
    if not eligibility.eligible:
        log.warning("discarding local setup recovery record for ineligible profile %s", profile.id)
        local_setup_operation.clear()
        return False

    _flight.result = None
    _flight.eligibility = eligibility
    _flight.kind = operation.kind
    _flight.stage = "Waiting to resume local model setup"

    async def resume() -> dict:
        while _flight.operation_lock is None:
            lock = local_setup_operation.try_lock()
            if lock is not None:
                _flight.operation_lock = lock
                break
            await asyncio.sleep(0.25)
        return await _run_flight(
            eligibility,
            profile=profile,
            into_own_omlx=operation.kind == "starter",
        )

    _flight.task = asyncio.create_task(resume())
    _flight.task.add_done_callback(_observe_setup_task)
    _flight.task.add_done_callback(lambda _task: _notify_models_changed())
    _emit("resumed", profile=profile.id, kind=operation.kind)
    return True


async def begin_local_setup(
    *,
    no_subscription_only: bool = False,
    automatic: bool = False,
    profile_id: str | None = None,
) -> dict:
    """Start the one setup flight without blocking the caller, or say why not.

    The profile, eligibility and subscription are each decided once, here; an omitted
    ``profile_id`` is the default profile. A request made while a flight runs reports
    that flight; any other request clears the previous terminal result before it is decided.
    """
    if _running():
        return _in_progress_result(_flight.eligibility)
    if local_setup_operation.read() is not None:
        if await resume_pending_setup():
            return _in_progress_result(_flight.eligibility)
        return local_setup_error("CORSIA could not resume the local model setup safely.", profile_id or DEFAULT_PROFILE.id)
    ram_bytes = await asyncio.to_thread(host_ram_bytes)
    foreign = profile_id is not None and await asyncio.to_thread(existing_install_is_foreign)
    # Refused before anything is cleared or started: the caller reads the reason.
    profile = resolve_profile(profile_id, ram_bytes, foreign)
    _flight.result = None
    subscription = await _subscription_available() if no_subscription_only else None
    eligibility = profile_eligibility(profile, ram_bytes)
    if subscription is not None:
        source = "Claude Code" if subscription.provider == "claude-code" else "Codex"
        account = f" as {subscription.label}" if subscription.label else ""
        return _skipped(
            "subscription_available",
            f"{source} is signed in{account}, so the local model is skipped. You can add one from the app.",
            eligibility,
        )
    notice = await _limited_subscription_message() if no_subscription_only else None
    if not eligibility.eligible:
        if notice:
            message = "This Mac can't run a local model well. Connect another subscription or an API key in the app."
            return _skipped("ineligible", f"{notice}\n{message}", eligibility)
        message = "This Mac can't run a local model well. Connect a subscription or an API key in the app."
        if automatic:
            return _skipped("ineligible", message, eligibility)
        _emit("skipped", reason="ineligible", profile=profile.id)
        return {**local_setup_error(message, profile.id), "eligibility": eligibility.__dict__, "progress": []}
    if _running():
        return _in_progress_result(_flight.eligibility)
    if not _claim_operation(profile, "setup"):
        if await resume_pending_setup():
            return _in_progress_result(_flight.eligibility)
        return _in_progress_result(eligibility)
    _flight.stage = "Installing"
    _flight.kind = "setup"
    _flight.eligibility = eligibility
    _flight.task = asyncio.create_task(_run_flight(eligibility, profile=profile))
    _flight.task.add_done_callback(_observe_setup_task)
    _flight.task.add_done_callback(lambda _task: _notify_models_changed())
    _emit("started", profile=profile.id)
    return _in_progress_result(eligibility, notice=notice)


from llm.local_setup_existing import (  # noqa: E402  (reads this module's names at call time)
    adopt_existing_omlx, begin_add_starter_model, download_into_own_omlx,
)
from llm.local_setup_new_omlx import _setup_new_omlx  # noqa: E402  (reads this module's names at call time)
