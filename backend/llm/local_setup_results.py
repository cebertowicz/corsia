"""Result and error shaping for local setup, free of flight state."""

from __future__ import annotations

import subprocess

import httpx
from fastapi import HTTPException

#: The one command that sets up, resumes or retries local setup; oMLX's recommender picks the model.
SETUP_COMMAND = "corsia local setup"
CAUSES = frozenset({"connection", "disk_full", "memory", "other", "own_omlx_upgrade_required"})


def local_setup_error(message: str) -> dict:
    return {
        "ok": False,
        "message": message,
        "next_action": f"Run `{SETUP_COMMAND}` again after fixing this, or add a subscription/API key.",
    }


def task_from_payload(payload: object, task_id: str) -> dict | None:
    if isinstance(payload, dict):
        candidates = payload.get("tasks") or payload.get("data") or []
    else:
        candidates = payload
    if not isinstance(candidates, list):
        return None
    return next((task for task in candidates
                 if isinstance(task, dict) and task.get("task_id") == task_id), None)


def failure_message(exc: Exception) -> str:
    """A cause the CLI and sources panel can show for any setup failure."""
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, ValueError):
        return "oMLX returned an unreadable response."
    if isinstance(exc, (OSError, subprocess.SubprocessError, httpx.HTTPError, RuntimeError)):
        return str(exc)
    return f"Local model setup failed unexpectedly ({type(exc).__name__}); see the backend log."


def failure_cause(exc: Exception) -> str:
    """Classify local-flight exceptions into stable setup-status cause codes."""
    from llm.local_setup_disk import DiskSpaceError
    from llm.local_setup_ownership import OwnOmlxUpgradeRequired

    if isinstance(exc, OwnOmlxUpgradeRequired):
        return "own_omlx_upgrade_required"
    if isinstance(exc, DiskSpaceError):
        return "disk_full"
    if isinstance(exc, MemoryError):
        return "memory"
    if isinstance(exc, (httpx.NetworkError, httpx.TimeoutException, ConnectionError, TimeoutError)):
        return "connection"
    return "other"


class DownloadProgress:
    """When a download line is due: each new ``STEP`` percent and, when set, every ``SECONDS``.

    The line shows the step (the app's stage text) unless ``SECONDS`` is set
    (the installer's terminal), when it shows the exact percent so a timed
    repeat still moves.
    """

    STEP = 10
    SECONDS: float | None = None

    def __init__(self, now: float) -> None:
        self.step, self.seconds, self.reported_at = self.STEP, self.SECONDS, now
        self.reported_step: int | None = None
        self.previous_downloaded: float | None = None
        self.previous_at = now
        self.bytes_per_second: float | None = None
        self.last_advance_at = now
        self.eta_seconds: int | None = None
        self.stalled = False

    def observe(self, downloaded: object, total: object, now: float) -> None:
        """Smooth a measured transfer rate; never manufacture an ETA before progress."""
        if not (isinstance(downloaded, (int, float)) and isinstance(total, (int, float)) and total > 0):
            self.eta_seconds = None
            return
        current = float(downloaded)
        if self.previous_downloaded is not None:
            elapsed = now - self.previous_at
            advanced = current - self.previous_downloaded
            if elapsed > 0 and advanced > 0:
                sample = advanced / elapsed
                self.bytes_per_second = sample if self.bytes_per_second is None else self.bytes_per_second * 0.75 + sample * 0.25
                self.last_advance_at = now
                self.stalled = False
            elif now - self.last_advance_at >= 30:
                self.stalled = True
                self.eta_seconds = None
        self.previous_downloaded, self.previous_at = current, now
        if self.bytes_per_second and not self.stalled:
            self.eta_seconds = max(0, round((float(total) - current) / self.bytes_per_second))

    def due(self, downloaded: object, total: object, now: float) -> int | None:
        if not (isinstance(downloaded, (int, float)) and isinstance(total, (int, float)) and total):
            return None
        percent = min(100, max(0, int(downloaded / total * 100)))
        step = percent // self.step * self.step
        timed = self.seconds is not None and now - self.reported_at >= self.seconds
        if not ((step and step != self.reported_step) or timed):
            return None
        self.reported_step, self.reported_at = step, now
        return percent if self.seconds is not None else step
