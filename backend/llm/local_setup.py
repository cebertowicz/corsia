"""Canonical local-model setup: one flight that installs a named local profile.

One explicit setup action makes at most one new oMLX HF-download request. oMLX
owns the persisted download task. The backend server is the only owner of a
setup flight: one in-process record holds the running task, its stage and the
last terminal result, so a server restart ends the flight and clears the result.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import httpx

from llm.local_setup_gates import Eligibility, host_ram_bytes, profile_eligibility, resolve_profile
from llm.local_setup_gates import limited_subscription_message as _limited_subscription_message
from llm.local_setup_gates import subscription_available as _subscription_available
from llm.local_setup_results import failure_message as _failure_message
from llm.local_setup_results import DownloadProgress, local_setup_error, setup_command
from llm.local_setup_results import task_from_payload as _task_from_payload
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
from llm.local_profiles import DEFAULT_PROFILE, OMLX_GLOBAL_SETTINGS, LocalProfile, format_size

LOCAL_HOST = "127.0.0.1"
# A first start on a new Mac imports MLX and preloads pinned models before it answers.
HEALTH_WAIT_SECONDS = 120.0
LOCAL_SERVER_NAME = "This Mac"
DOWNLOAD_WAIT_SECONDS = 60 * 60
DOWNLOAD_POLL_SECONDS = 2.0
# Past the wait the one flight keeps following at a slower poll (2 per minute,
# against 30 per minute while waiting) and stops after DOWNLOAD_FOLLOW_SECONDS.
DOWNLOAD_BACKGROUND_POLL_SECONDS = 30.0
DOWNLOAD_FOLLOW_SECONDS = 24 * 60 * 60
EVENT_CATEGORY = "local_setup"

log = logging.getLogger(__name__)


async def _wait_for_health(port: int) -> None:
    """Wait for oMLX on ``port``; on timeout, say what oMLX's own log says."""
    from llm.providers.omlx_endpoints import health_url

    deadline = time.monotonic() + HEALTH_WAIT_SECONDS
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get(health_url(LOCAL_HOST, port))).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    detail = await asyncio.to_thread(omlx_log_detail)
    raise RuntimeError(
        f"oMLX didn't answer on {LOCAL_HOST}:{port} within {int(HEALTH_WAIT_SECONDS)} seconds.{detail}",
    )


async def _register_loopback_server(*, port: int, api_key: str | None = None) -> str:
    """Use the existing manual-registration path after the loopback health check.

    A foreign oMLX is registered without a key at its configured port. CORSIA's
    own instance is registered with its generated key before oMLX receives it.
    """
    from api.network_server_helpers import _add_omlx_server

    result = await _add_omlx_server(SimpleNamespace(
        hostname=LOCAL_HOST, port=port, name=LOCAL_SERVER_NAME, api_key=api_key,
    ))
    return str(result["server_id"])


async def _configure_global(server_id: str) -> None:
    """Write oMLX's server-wide settings and, once, the loopback-only auth flag."""
    from llm.providers.omlx_settings import save_global_settings

    pairs = {**OMLX_GLOBAL_SETTINGS, SKIP_AUTH_SETTING: True}
    result = await save_global_settings({"server_id": server_id}, pairs)
    if not result.get("ok"):
        raise RuntimeError(str(result.get("message", "oMLX could not save global settings.")))


async def _configure_model_settings(server_id: str, context_window: int, profile: LocalProfile) -> None:
    from llm.providers.omlx_endpoints import admin_request

    payload = {**profile.model_settings, "max_context_window": context_window}
    response = await admin_request(
        {"server_id": server_id}, "PUT", f"/models/{profile.model}/settings", json=payload, timeout=15.0,
    )
    if response.status_code != 200:
        raise RuntimeError(f"oMLX could not save settings for {profile.display_name} (HTTP {response.status_code}).")


async def _wait_for_download(server_id: str, progress: Callable[[str], None],
                             profile: LocalProfile) -> None:
    """Request one persisted HF task and follow it with the one poll loop.

    The foreground wait ends into background following, not a failure: the
    same loop keeps polling slowly, so its completion reaches the flight, the
    only owner of the configure and catalog-refresh transition.
    """
    from llm.providers.omlx_endpoints import admin_request

    cfg = {"server_id": server_id}
    response = await admin_request(
        cfg, "POST", "/hf/download", json={"repo_id": profile.repository, "hf_token": ""}, timeout=15.0,
    )
    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"oMLX could not start the model download (HTTP {response.status_code}).")
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("success"):
        raise RuntimeError("oMLX did not accept the model download request.")
    task = payload.get("task")
    task_id = task.get("task_id") if isinstance(task, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError("oMLX did not return a download task id.")

    started = time.monotonic()
    tracker = DownloadProgress(started)
    while True:
        tasks_response = await admin_request(cfg, "GET", "/hf/tasks", timeout=15.0)
        if tasks_response.status_code != 200:
            raise RuntimeError("oMLX could not read download progress.")
        task = _task_from_payload(tasks_response.json(), task_id)
        if task is None:
            raise RuntimeError(f"oMLX no longer reports the {profile.display_name} download task.")
        status = str(task.get("status") or "").lower()
        shown = tracker.due(task.get("downloaded_size"), task.get("total_size"), time.monotonic())
        if shown is not None:
            progress(f"Downloading {profile.model} ({format_size(profile.download_size_bytes)}, {shown}%)")
        if status in {"completed", "complete", "success", "succeeded"}:
            return
        if status in {"failed", "error", "cancelled", "canceled"}:
            detail = task.get("error") or "The oMLX download task failed."
            raise RuntimeError(str(detail))
        elapsed = time.monotonic() - started
        if elapsed >= DOWNLOAD_FOLLOW_SECONDS:
            raise StillDownloading()
        if elapsed >= DOWNLOAD_WAIT_SECONDS and not _flight.following:
            _flight.following = True
            progress(f"Still downloading {profile.model}; it will finish in the background")
        await asyncio.sleep(
            DOWNLOAD_BACKGROUND_POLL_SECONDS if _flight.following else DOWNLOAD_POLL_SECONDS,
        )


async def _download_dir(server_id: str) -> Path | None:
    """Where this oMLX downloads to, as it reports it; None when it can't be read.

    ``model.model_dir`` is oMLX's own resolution of ``--model-dir``, its settings file
    and its default; oMLX ``server.py`` hands that directory to ``HFDownloader``.
    """
    from llm.providers.omlx_settings import get_settings

    settings = (await get_settings({"server_id": server_id})).get("settings")
    model = settings.get("model") if isinstance(settings, dict) else None
    path = model.get("model_dir") if isinstance(model, dict) else None
    return Path(path) if isinstance(path, str) and path else None


async def _download(server_id: str, report: Callable[[str], None], profile: LocalProfile) -> None:
    """Check the disk oMLX downloads to, then request and follow the profile's download."""
    model_dir = await _download_dir(server_id)
    await asyncio.to_thread(require_free_disk, profile.download_size_bytes, model_dir, profile.repository)
    report(f"Downloading {profile.model} ({format_size(profile.download_size_bytes)})")
    await _wait_for_download(server_id, report, profile)


class StillDownloading(Exception):
    """The download outlived what CORSIA follows; oMLX is still running it."""


@dataclass
class _Flight:
    task: asyncio.Task | None = None
    stage: str | None = None
    result: dict | None = None
    eligibility: Eligibility | None = None
    following: bool = False
    kind: str | None = None


_flight = _Flight()


def _running() -> bool:
    return _flight.task is not None and not _flight.task.done()


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
    return result


def flight_task() -> asyncio.Task | None:
    """The flight's task for lifespan shutdown to cancel and join, else None."""
    return _flight.task


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
