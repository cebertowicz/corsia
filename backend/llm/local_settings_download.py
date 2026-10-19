"""Settings-owned local-model download flight.

The flight borrows setup's sole operation record and oMLX task follower while
keeping its terminal state out of setup's marker and fallback semantics.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from api.network_server_helpers import notify_cached_model_loaded
from llm import local_downloads
from llm.local_model import LocalModel
from llm.local_setup_download import StillDownloading

log = logging.getLogger(__name__)


async def _managed_download(report: Callable[[str], None], model: LocalModel) -> str:
    """Download through CORSIA's managed oMLX only."""
    from llm import local_setup as ls
    from llm.local_setup_new_omlx import start_managed_omlx

    server_id, _port = await start_managed_omlx(report)
    await ls._download(server_id, report, model)
    return server_id


async def _auto_load(server_id: str, model: LocalModel, *, allowed: bool) -> None:
    """Load only a downloaded CORSIA-managed model that the existing memory gate permits."""
    if not allowed:
        log.warning("Skipping automatic local-model load: CORSIA does not manage this oMLX")
        return
    from llm.providers.omlx_endpoints import OmlxAdminError, admin_request

    try:
        response = await admin_request({"server_id": server_id}, "GET", "/models", timeout=15.0)
    except OmlxAdminError:
        log.warning("Skipping automatic local-model load: oMLX models request failed")
        return
    if response.status_code != 200:
        log.warning("Skipping automatic local-model load: oMLX models request returned %s", response.status_code)
        return
    try:
        payload = response.json()
    except ValueError:
        log.warning("Skipping automatic local-model load: oMLX returned malformed models data")
        return
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        log.warning("Skipping automatic local-model load: oMLX returned malformed models data")
        return
    row = next((item for item in rows if isinstance(item, dict) and item.get("id") == model.model), None)
    if row is None:
        log.warning("Skipping automatic local-model load: downloaded model is missing from oMLX")
        return
    size = row.get("estimated_size")
    if not isinstance(size, (int, float)) or size <= 0:
        log.warning("Skipping automatic local-model load: downloaded model has no usable size")
        return
    from infra.host_resources import check_can_load
    from llm.providers.omlx_lifecycle import load_model
    from llm.network_discovery import discover_and_notify

    step = "memory check"
    try:
        decision = await check_can_load(float(size) / 1024**3, "omlx")
        if not decision.get("ok"):
            log.warning("Skipping automatic local-model load: not enough free memory")
            return
        step = "model load"
        result = await load_model({"server_id": server_id}, model.model)
        if not result.get("ok"):
            log.warning("Couldn't automatically load downloaded local model")
            return
        notify_cached_model_loaded(server_id, model.model)
        step = "catalog refresh"
        await discover_and_notify(server_id)
    except Exception:
        log.warning("Skipping automatic local-model load after %s failed", step, exc_info=True)


async def _run_flight(model: LocalModel) -> dict:
    """Run or resume the sole Settings flight with one reporting and cleanup path."""
    from llm import local_setup as ls

    events: list[str] = []

    def report(value: str) -> None:
        ls._flight.stage = value
        ls._notify_models_changed()
        events.append(value)

    ls._flight.following = False
    try:
        result = await run(report, model)
    except asyncio.CancelledError:
        ls._release_operation_lock()
        raise
    except StillDownloading:
        result = ls._stopped_following(model, events)
    except Exception as exc:
        ls.log.exception("Settings model download failed")
        ls._emit("failed", kind="settings", model=model.repository, error_type=type(exc).__name__)
        result = {"ok": False, "message": f"Couldn't download {model.display_name}. Try again.", "detail": str(exc), "progress": events}
    else:
        if result.get("ok") and not result.get("downloading"):
            ls._emit("completed", kind="settings", model=model.repository)
    if not result.get("ok") and not result.get("downloading"):
        local_downloads.write(local_downloads.Download(
            model, status="failed", detail=str(result.get("detail") or result.get("message") or ""),
        ))
    return ls.local_setup_operation.finish(ls._flight, result)


async def begin(model: LocalModel) -> dict:
    """Start the single Settings flight, or report the already-owned operation."""
    from llm import local_setup as ls
    from llm import local_setup_operation

    if ls._running():
        return ls._in_progress_result()
    if local_setup_operation.read() is not None:
        if await ls.resume_pending_setup():
            return ls._in_progress_result()
        return {"ok": False, "reason": "recovery_failed", "message": "Couldn't resume the local model download. Try again."}
    if not await asyncio.to_thread(ls.local_memory_ok):
        return {"ok": False, "reason": "mac_cannot_run", "message": "This computer can't run a local model well."}
    if not ls._claim_operation("settings", model):
        await ls.resume_pending_setup()
        return ls._in_progress_result()
    ls._flight.result = None
    ls._flight.stage = "Preparing download"

    async def flight() -> dict:
        return await _run_flight(model)

    ls._flight.task = asyncio.create_task(flight())
    ls._flight.task.add_done_callback(ls._observe_setup_task)
    ls._flight.task.add_done_callback(lambda _task: ls._notify_models_changed())
    ls._emit("started", model=model.repository, kind="settings")
    return ls._in_progress_result()


async def resume(model: LocalModel) -> dict:
    """Re-follow a persisted Settings download after restart without adopting setup."""
    from llm import local_setup as ls

    await ls.local_setup_operation.wait_for_lock(ls._flight)
    ls._flight.kind = "settings"
    ls._flight.model = model
    ls._flight.stage = "Waiting to resume local model download"
    return await _run_flight(model)


async def run(report: Callable[[str], None], model: LocalModel) -> dict:
    """Complete a Settings download and preserve a separate terminal result."""
    from llm import local_setup as ls
    from llm.local_setup_runtime import apply_optimal_settings, configure_model_settings
    from llm.catalog_network_models import discover_network_models

    events: list[str] = []

    def stage(value: str) -> None:
        report(value)
        events.append(value)

    server_id = await _managed_download(stage, model)
    # An applied benchmark owns the one settings PUT and carries CORSIA's
    # required fields first. A missing/offline benchmark falls back to the
    # ordinary per-model writer without treating that best-effort step as a
    # download failure.
    optimal = await apply_optimal_settings(server_id, model)
    if optimal.status != "applied" and not optimal.writer_attempted:
        await configure_model_settings(server_id, model)
    speed = optimal.speed if optimal.status == "applied" else None
    stage("Refreshing model catalog")
    refreshed = await discover_network_models(server_id)
    if not refreshed.get("ok"):
        raise RuntimeError(refreshed.get("error") or "The local model catalog could not refresh.")
    # The durable record belongs to this exact local server; UI metadata never crosses servers.
    local_downloads.write(local_downloads.Download(model, speed=speed, server_id=server_id))
    await _auto_load(server_id, model, allowed=True)
    return {"ok": True, "server_id": server_id, "model": model.model, "progress": events, "speed": speed}
