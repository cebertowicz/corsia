"""Bounded, per-vendor installation jobs for the supported CLI harnesses."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from api import version as app_version
from api.provider_sources import recheck_sources_once
from cli_location import login_shell_binary, package_manager_for, path_shadowed_binary, service_binary, service_path
from connections.vendor_accounts import VENDORS
from infra.auth import require_user
from infra.failure_reference import new_reference
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
_CANCEL_GRACE_SECONDS = 3
_MAX_SCRIPT_BYTES = 1_000_000
_MAX_LOG_CHARS = 2_000
_SECRET_VALUE = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|authorization)\b(\s*[:=]\s*)(\S+)"
)

NO_INTERNET = "no internet connection"
DOWNLOAD_UNAVAILABLE = "the download service cannot answer"
DOWNLOAD_REFUSED = "the download site refused the request"
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
    ref: str = field(default_factory=new_reference)
    path_shadowed_by: str | None = None
    task: asyncio.Task | None = None
    # Cancel: the installer runs in its own process group so the whole tree can be stopped. `lock` orders
    # a cancel request against the launch, so a cancel never lands between the check and the Popen.
    process: subprocess.Popen[bytes] | None = None
    cancel_requested: bool = False
    cancelled: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    settled: asyncio.Event = field(default_factory=asyncio.Event)


_jobs: dict[str, InstallJob] = {}


def install_cause(reason: str | None) -> str | None:
    """The stable install failure code; UI behavior must not depend on English copy."""
    if reason is None:
        return None
    if reason == NO_INTERNET:
        return "connection"
    if reason == DOWNLOAD_REFUSED:
        return "site_refused"
    if reason == DOWNLOAD_UNVERIFIED:
        return "download_unverified"
    return "installer_failed"


def _status(vendor: str) -> dict[str, str | None]:
    """Return the public state shape for one vendor's installation job."""
    job = _jobs.get(vendor)
    if job is None:
        return {"state": "idle", "reason": None, "ref": None, "path_shadowed_by": None}
    result = {
        "state": job.state, "reason": job.reason,
        "ref": job.ref if job.state == "failed" else None, "path_shadowed_by": job.path_shadowed_by,
    }
    if job.state == "failed":
        result["cause"] = install_cause(job.reason)
    return result


def _installer_request(vendor: str) -> urllib.request.Request:
    """Build the official installer request with CORSIA's running app version."""
    version = app_version._app_version(app_version._read_frontend_build_info()) or "unknown"
    return urllib.request.Request(
        INSTALL_URLS[vendor], headers={"Accept": "text/plain", "User-Agent": f"CORSIA/{version}"}
    )


def _download_script(vendor: str) -> Path:
    """Download and minimally verify an official shell installer into a temp file."""
    request = _installer_request(vendor)
    try:
        with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            script = response.read(_MAX_SCRIPT_BYTES + 1)
    except TimeoutError as exc:
        raise InstallerFailure(TIMED_OUT) from exc
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            reason = DOWNLOAD_REFUSED
        else:
            reason = DOWNLOAD_UNAVAILABLE
        raise InstallerFailure(reason) from exc
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


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    """Stop an installer and everything it started: SIGTERM to its group, SIGKILL if it outlasts a short grace."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=_CANCEL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _request_cancel(job: InstallJob) -> None:
    """Ask a running job to stop. A job that has not launched yet never will; one that has is stopped."""
    with job.lock:
        job.cancel_requested = True
        process = job.process
    if process is not None and process.poll() is None:
        _stop_group(process)


def _run_installer(vendor: str, job: InstallJob) -> str | None:
    """Run one downloaded installer and return a safe failure reason, if any.

    A stop the person asked for settles as `job.cancelled`, never as a failure. An installer that
    finished successfully counts as done even when a cancel arrived at the same moment.
    """
    try:
        script = _download_script(vendor)
    except InstallerFailure as failure:
        job.cancelled = job.cancel_requested
        return None if job.cancelled else str(failure)
    try:
        with job.lock:
            if job.cancel_requested:
                job.cancelled = True
                return None
            try:
                process = subprocess.Popen(
                    ["bash", str(script)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=_installer_env(), start_new_session=True,
                )
            except OSError:
                return INSTALLER_STOPPED
            job.process = process
        try:
            stdout, stderr = process.communicate(timeout=_INSTALL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _stop_group(process)
            process.communicate()
            return TIMED_OUT
        if not process.returncode:
            return None
        if job.cancel_requested:
            job.cancelled = True
            return None
        return _failure_reason(vendor, subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr))
    finally:
        script.unlink(missing_ok=True)


async def _complete_job(vendor: str, job: InstallJob) -> None:
    """Settle an installer job, refresh sources once, and notify live model consumers."""
    try:
        job.reason = await asyncio.to_thread(_run_installer, vendor, job)
    except Exception:
        log.exception("CLI installer for %s stopped unexpectedly", vendor)
        job.reason = INSTALLER_STOPPED
    job.state = "cancelled" if job.cancelled else "failed" if job.reason else "done"
    job.settled.set()
    if job.state == "done":
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
    """Return exactly idle, running, done, failed, or cancelled state for one CLI installer."""
    _valid_vendor(vendor)
    return _status(vendor)


@router.delete("/{vendor}/install")
async def cancel_install(vendor: str, _user: dict = Depends(require_user)) -> dict[str, str | None]:
    """Stop a running CLI installer and return the state it settled in.

    Idle, done, failed and cancelled jobs are left alone. A cancel that races the installer's
    completion returns whichever single state the job actually settled in.
    """
    _valid_vendor(vendor)
    job = _jobs.get(vendor)
    if job is None or job.state != "running":
        return _status(vendor)
    await asyncio.to_thread(_request_cancel, job)
    try:
        await asyncio.wait_for(job.settled.wait(), timeout=_CANCEL_GRACE_SECONDS + 5)
    except TimeoutError:
        log.warning("CLI installer for %s did not settle after a cancel request", vendor)
    return _status(vendor)


async def _start_job(vendor: str, operation: str) -> dict[str, str | None] | JSONResponse:
    existing = _jobs.get(vendor)
    if existing and existing.state == "running":
        return _status(vendor)
    job = InstallJob()
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
