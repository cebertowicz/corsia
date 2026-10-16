"""Bounded, per-vendor installation jobs for the supported CLI harnesses."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from api.provider_sources import recheck_sources_once
from cli_location import login_shell_binary, package_manager_for, path_shadowed_binary, service_binary, service_path
from connections.vendor_accounts import VENDORS
from infra.auth import require_user
from infra.state.ws import schedule_models_changed

router = APIRouter(prefix="/api/providers/cli", tags=["providers"])
log = logging.getLogger(__name__)

INSTALL_URLS = {
    "claude-code": "https:" "//claude.ai/install.sh",
    "codex": "https:" "//chatgpt.com/codex/install.sh",
}
# The only service variables a downloaded installer may read; everything else
# (tokens, keys, other credentials) stays out of its environment.
_ENV_PASSTHROUGH = ("HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL")
_DOWNLOAD_TIMEOUT_SECONDS = 30
_INSTALL_TIMEOUT_SECONDS = 180
_MAX_SCRIPT_BYTES = 1_000_000
_MAX_LOG_CHARS = 2_000
_SECRET_VALUE = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|authorization)\b(\s*[:=]\s*)(\S+)"
)

NO_INTERNET = "no internet connection"
DOWNLOAD_UNAVAILABLE = "the download service didn't answer"
DOWNLOAD_UNVERIFIED = "the download didn't verify"
TIMED_OUT = "it took too long"
INSTALLER_STOPPED = "the installer stopped"


class InstallerFailure(RuntimeError):
    """A safe, user-facing reason why an installation job could not finish."""


@dataclass
class InstallJob:
    """The current in-memory state for one vendor installation or update job."""

    state: str = "running"
    reason: str | None = None
    operation: str = "install"
    path_shadowed_by: str | None = None
    task: asyncio.Task | None = None


_jobs: dict[str, InstallJob] = {}


def _status(vendor: str) -> dict[str, str | None]:
    """Return the public state shape for one vendor's installation job."""
    job = _jobs.get(vendor)
    return ({"state": job.state, "reason": job.reason, "path_shadowed_by": job.path_shadowed_by}
            if job else {"state": "idle", "reason": None, "path_shadowed_by": None})


def _download_script(vendor: str) -> Path:
    """Download and minimally verify an official shell installer into a temp file."""
    request = urllib.request.Request(INSTALL_URLS[vendor], headers={"Accept": "text/plain"})
    try:
        with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            script = response.read(_MAX_SCRIPT_BYTES + 1)
    except TimeoutError as exc:
        raise InstallerFailure(TIMED_OUT) from exc
    except urllib.error.HTTPError as exc:
        raise InstallerFailure(DOWNLOAD_UNAVAILABLE) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise InstallerFailure(NO_INTERNET) from exc
    if len(script) > _MAX_SCRIPT_BYTES or not script.startswith(b"#!"):
        raise InstallerFailure(DOWNLOAD_UNVERIFIED)
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="corsia-cli-install-", suffix=".sh", delete=False) as file:
            path = Path(file.name)
            file.write(script)
    except OSError as exc:
        log.exception("CLI installer script could not be written to a temp file")
        if path is not None:
            path.unlink(missing_ok=True)
        raise InstallerFailure(INSTALLER_STOPPED) from exc
    return path


def _installer_env() -> dict[str, str]:
    """Build the installer's minimal environment around the launchd service's executable PATH."""
    env = {name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ}
    return {**env, "PATH": service_path(), "CODEX_NON_INTERACTIVE": "1"}


def _bounded_output(result: subprocess.CompletedProcess[bytes]) -> str:
    """Return only the bounded installer output needed for classification and safe logs."""
    return (result.stdout + result.stderr).decode(errors="replace")[-_MAX_LOG_CHARS:]


def _failure_reason(vendor: str, result: subprocess.CompletedProcess[bytes]) -> str:
    """Classify known installer output without exposing it through the API."""
    output = _bounded_output(result)
    safe_output = _SECRET_VALUE.sub(r"\1\2[redacted]", output)
    log.info("CLI installer ended with return code %s: %s", result.returncode, safe_output)
    if "Checksum verification failed" in output:
        return DOWNLOAD_UNVERIFIED
    if vendor == "claude-code" and "Failed to get a valid version" in output:
        return DOWNLOAD_UNAVAILABLE
    if vendor == "codex" and "Could not fetch" in output:
        return NO_INTERNET
    return INSTALLER_STOPPED


def _run_installer(vendor: str) -> str | None:
    """Run one downloaded installer and return a safe failure reason, if any."""
    try:
        script = _download_script(vendor)
    except InstallerFailure as failure:
        return str(failure)
    try:
        try:
            result = subprocess.run(
                ["bash", str(script)], stdin=subprocess.DEVNULL, capture_output=True,
                env=_installer_env(), timeout=_INSTALL_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired:
            return TIMED_OUT
        except OSError:
            return INSTALLER_STOPPED
        if result.returncode:
            return _failure_reason(vendor, result)
        return None
    finally:
        script.unlink(missing_ok=True)


async def _complete_job(vendor: str, job: InstallJob) -> None:
    """Settle an installer job, refresh sources once, and notify live model consumers."""
    try:
        job.reason = await asyncio.to_thread(_run_installer, vendor)
    except Exception:
        log.exception("CLI installer for %s stopped unexpectedly", vendor)
        job.reason = INSTALLER_STOPPED
    job.state = "failed" if job.reason else "done"
    if not job.reason:
        try:
            job.path_shadowed_by = await asyncio.to_thread(path_shadowed_binary, vendor)
        except Exception:
            log.warning("CLI location check after installation did not complete")
    try:
        await recheck_sources_once()
    except Exception:
        log.warning("model source recheck after CLI installation did not complete")
    schedule_models_changed()


def _valid_vendor(vendor: str) -> None:
    """Reject a URL vendor that is not a built-in CLI harness."""
    if vendor not in VENDORS:
        raise HTTPException(404, "CLI vendor not found")


@router.get("/{vendor}/install")
async def get_install(vendor: str, _user: dict = Depends(require_user)) -> dict[str, str | None]:
    """Return exactly idle, running, done, or failed state for one CLI installer."""
    _valid_vendor(vendor)
    return _status(vendor)


async def _start_job(vendor: str, operation: str) -> dict[str, str | None] | JSONResponse:
    existing = _jobs.get(vendor)
    if existing and existing.state == "running":
        return _status(vendor)
    job = InstallJob(operation=operation)
    _jobs[vendor] = job
    job.task = asyncio.create_task(_complete_job(vendor, job))
    return _status(vendor)


@router.post("/{vendor}/install", response_model=None)
async def start_install(vendor: str, _user: dict = Depends(require_user)) -> dict[str, str | None] | JSONResponse:
    """Start one CLI installer, returning an already-running job instead of another."""
    _valid_vendor(vendor)
    existing = _jobs.get(vendor)
    if existing and existing.state == "running":
        return _status(vendor)
    if service_binary(vendor):
        return JSONResponse(status_code=409, content={"reason": "already_installed"})
    try:
        hidden = await asyncio.to_thread(login_shell_binary, vendor)
    except TimeoutError:
        hidden = None
    if hidden:
        return JSONResponse(status_code=409, content={"reason": "invisible_binary"})
    return await _start_job(vendor, "install")


@router.post("/{vendor}/update", response_model=None)
async def start_update(vendor: str, _user: dict = Depends(require_user)) -> dict[str, str | None] | JSONResponse:
    """Run the vendor's official installer again for a CLI the service already resolves."""
    _valid_vendor(vendor)
    binary = service_binary(vendor)
    if not binary:
        return JSONResponse(status_code=409, content={"reason": "not_installed"})
    if await asyncio.to_thread(package_manager_for, vendor, binary):
        return JSONResponse(status_code=409, content={"reason": "package_managed"})
    return await _start_job(vendor, "update")
