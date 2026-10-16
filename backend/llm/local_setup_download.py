"""oMLX download-task discovery and polling for local setup."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable

from llm import local_setup_operation
from llm.local_profiles import LocalProfile, format_size
from llm.local_setup_results import DownloadProgress, task_from_payload

DOWNLOAD_WAIT_SECONDS = 60 * 60
DOWNLOAD_POLL_SECONDS = 2.0
DOWNLOAD_BACKGROUND_POLL_SECONDS = 30.0
DOWNLOAD_FOLLOW_SECONDS = 24 * 60 * 60


class StillDownloading(Exception):
    """The download outlived what CORSIA follows; oMLX is still running it."""


async def wait_for_download(
    server_id: str,
    progress: Callable[[str], None],
    profile: LocalProfile,
    *,
    record_task: Callable[[LocalProfile, str], None],
    flight: Any,
    wait_seconds: float = DOWNLOAD_WAIT_SECONDS,
    poll_seconds: float = DOWNLOAD_POLL_SECONDS,
    background_poll_seconds: float = DOWNLOAD_BACKGROUND_POLL_SECONDS,
    follow_seconds: float = DOWNLOAD_FOLLOW_SECONDS,
) -> None:
    """Request one persisted HF task and follow it without duplicating requests."""
    from llm.providers.omlx_endpoints import admin_request

    cfg = {"server_id": server_id}
    operation = local_setup_operation.read()
    task_id = operation.task_id if operation and operation.profile_id == profile.id else None
    if task_id is None:
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
        record_task(profile, task_id)

    started = time.monotonic()
    tracker = DownloadProgress(started)
    while True:
        tasks_response = await admin_request(cfg, "GET", "/hf/tasks", timeout=15.0)
        if tasks_response.status_code != 200:
            raise RuntimeError("oMLX could not read download progress.")
        task = task_from_payload(tasks_response.json(), task_id)
        if task is None:
            raise RuntimeError(f"oMLX no longer reports the {profile.display_name} download task.")
        status = str(task.get("status") or "").lower()
        shown = tracker.due(task.get("downloaded_size"), task.get("total_size"), time.monotonic())
        if shown is not None:
            progress(f"Downloading {profile.model} ({format_size(profile.download_size_bytes)}, {shown}%)")
        if status in {"completed", "complete", "success", "succeeded"}:
            return
        if status in {"failed", "error", "cancelled", "canceled"}:
            raise RuntimeError(str(task.get("error") or "The oMLX download task failed."))
        elapsed = time.monotonic() - started
        if elapsed >= follow_seconds:
            raise StillDownloading()
        if elapsed >= wait_seconds and not flight.following:
            flight.following = True
            progress(f"Still downloading {profile.model}; it will finish in the background")
        await asyncio.sleep(
            background_poll_seconds if flight.following else poll_seconds,
        )


async def download_dir(server_id: str) -> Path | None:
    """Return the model directory oMLX reports, or None when it cannot be read."""
    from llm.providers.omlx_settings import get_settings

    settings = (await get_settings({"server_id": server_id})).get("settings")
    model = settings.get("model") if isinstance(settings, dict) else None
    path = model.get("model_dir") if isinstance(model, dict) else None
    return Path(path) if isinstance(path, str) and path else None
