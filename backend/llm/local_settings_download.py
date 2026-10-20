"""Settings-owned local-model download flight.

The flight borrows setup's sole operation record and oMLX task follower while
keeping its terminal state out of setup's marker and fallback semantics.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from llm import local_downloads
from llm.local_keep_loaded import keep_loaded
from llm.local_model import LocalModel
from llm.local_setup_download import StillDownloading
from llm.local_setup_runtime import start_managed_omlx

log = logging.getLogger(__name__)


class DownloadedNotListed(RuntimeError):
    """The bytes are on disk but CORSIA could not finish listing the model."""


async def _note_reachable(server_id: str) -> None:
    """Report the just-reached server as online before refreshing its catalog."""
    from infra.db_async import run_in_db
    from llm import network_discovery as nd
    from llm.server_observation import record_reachable

    server = await run_in_db(nd.get_server, server_id)
    if server and server.get("status") == "offline":
        await record_reachable(server, loaded=None, rediscover=False)


async def _managed_download(report: Callable[[str], None], model: LocalModel) -> str:
    """Download through CORSIA's managed local server only."""
    from llm import local_setup as ls

    server_id, _port = await start_managed_omlx(report)
    await ls._download(server_id, report, model)
    return server_id


async def _run_flight(model: LocalModel) -> dict:
    """Run or resume the sole Settings flight with one reporting and cleanup path."""
    from llm import local_setup as ls

    events: list[str] = []

    def report(value: str) -> None:
        ls.report_stage(value)
        events.append(value)

    try:
        result = await run(report, model)
    except asyncio.CancelledError:
        ls._release_operation_lock()
        raise
    except StillDownloading:
        result = ls._stopped_following(model, events)
    except DownloadedNotListed as exc:
        ls.log.exception("Settings model downloaded but not listed")
        ls._emit("failed", kind="settings", model=model.repository, error_type=type(exc.__cause__ or exc).__name__)
        result = {
            "ok": False, "downloaded": True, "detail": str(exc), "progress": events,
            "message": f"{model.display_name} is downloaded but couldn't be added. Retry to finish adding it.",
        }
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
            downloaded=bool(result.get("downloaded")),
        ))
    return ls.finish_flight(result)



async def run(report: Callable[[str], None], model: LocalModel) -> dict:
    """Complete a Settings download and preserve a separate terminal result."""
    from llm.local_setup_runtime import apply_optimal_settings, configure_model_settings
    from llm.catalog_network_models import discover_network_models

    events: list[str] = []

    def stage(value: str) -> None:
        report(value)
        events.append(value)

    server_id = await _managed_download(stage, model)
    # From here the bytes are on disk: a failure is "downloaded, not listed", never "couldn't download".
    try:
        await _note_reachable(server_id)
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
    except Exception as exc:
        raise DownloadedNotListed(str(exc)) from exc
    # The durable record belongs to this exact local server; UI metadata never crosses servers.
    local_downloads.write(local_downloads.Download(model, speed=speed, server_id=server_id))
    stage("Loading model")
    await keep_loaded(server_id, model)
    return {"ok": True, "server_id": server_id, "model": model.model, "progress": events, "speed": speed}
