"""Loopback-only, pre-auth first-run setup.

These routes deliberately expose only the facts needed before an account can
exist. They are not a general unauthenticated provider or account API.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import sys
from pathlib import Path

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from infra import accounts, config
from infra.db_async import run_in_db
from infra.default_port import DEFAULT_BACKEND_PORT
from infra.local_connection import is_direct_loopback
from infra.time import utc_storage_iso_to_api

router = APIRouter(prefix="/api/setup", tags=["setup"])

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_TAURI_ORIGINS = frozenset({"tauri:" + "//localhost", "http:" + "//tauri.localhost"})
_MINIMUM_MACOS_MAJOR = 15


class FirstAccountRequest(BaseModel):
    """The account screen's fields; password confirmation remains client-side."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    email: str | None = Field(default=None, max_length=320)


def _loopback_host(request: Request) -> bool:
    """Require an exact literal loopback Host with CORSIA's backend port."""
    host = request.headers.get("host")
    if host is None:
        return False
    name, separator, port = host.rpartition(":")
    return (
        bool(separator)
        and name in _LOOPBACK_HOSTS
        and port == str(DEFAULT_BACKEND_PORT)
    )


def _trusted_request(request: Request) -> bool:
    """Require a direct local peer and literal loopback request target."""
    return is_direct_loopback(request) and _loopback_host(request)


def _trusted_json_post(request: Request) -> bool:
    """Apply the pre-auth POST transport boundary without reading its body."""
    if not _trusted_request(request):
        return False
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/json":
        return False
    origin = request.headers.get("origin")
    if origin is None:
        # Browsers attach Sec-Fetch-* metadata; a missing Origin is reserved
        # for a direct non-browser client that has already passed Host + JSON.
        return not any(name in request.headers for name in ("sec-fetch-mode", "sec-fetch-site"))
    allowed_origins = {
        *("http:" + f"//{host}:{DEFAULT_BACKEND_PORT}" for host in _LOOPBACK_HOSTS),
        *_TAURI_ORIGINS,
    }
    return origin in allowed_origins


def _macos_supported() -> bool:
    """Whether this Mac meets the approved macOS 15 floor."""
    if platform.system() != "Darwin":
        return False
    try:
        return int(platform.mac_ver()[0].split(".", 1)[0]) >= _MINIMUM_MACOS_MAJOR
    except (TypeError, ValueError):
        return False


def _app_location_class(executable: Path | None = None) -> str:
    """Expose a coarse package class, never the executable's path."""
    parts = (executable or Path(sys.executable)).parts
    if "Volumes" in parts:
        return "disk_image"
    if "Applications" in parts:
        return "applications"
    return "other"


def _work_folder_display(workspace: Path) -> str:
    """Return a home-abbreviated workspace label with no account name leakage."""
    try:
        relative = workspace.resolve().relative_to(Path.home().resolve())
    except ValueError:
        return f"…/{workspace.name}" if workspace.name else "…"
    return "~" if not relative.parts else f"~/{relative.as_posix()}"


def _seed_workspace_profile(name: str, email: str) -> None:
    """Use the CLI's existing profile-create-or-add-email behavior after account creation."""
    from infra.user_profile import add_profile_email, mac_timezone, write_user_profile

    if not write_user_profile(config.WORKSPACE, name, mac_timezone(), email):
        add_profile_email(config.WORKSPACE, email)


def _setup_status() -> dict:
    """Build the bounded pre-auth screen facts without exposing machine identity."""
    from llm.local_setup_gates import host_ram_bytes, profile_eligibility
    from llm.local_profiles import DEFAULT_PROFILE

    ram_bytes = host_ram_bytes()
    apple_silicon = platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}
    macos_supported = _macos_supported()
    profile_eligible = profile_eligibility(DEFAULT_PROFILE, ram_bytes).eligible
    try:
        free_disk_gb = round(shutil.disk_usage(config.STATE_ROOT).free / 1000**3, 1)
    except OSError:
        free_disk_gb = None
    return {
        "apple_silicon": apple_silicon,
        "macos_supported": macos_supported,
        "ram_tier": "eligible" if profile_eligible else "below_minimum",
        "free_disk_gb": free_disk_gb,
        "app_location": _app_location_class(),
        "local_eligible": apple_silicon and macos_supported and profile_eligible,
        "work_folder": _work_folder_display(config.WORKSPACE),
        "model_name": DEFAULT_PROFILE.display_name,
        "model_size_gb": round(DEFAULT_PROFILE.download_size_bytes / 1000**3, 1),
    }


@router.get("/status", response_model=None)
async def setup_status(request: Request) -> dict | Response:
    """Report only first-run screen facts to a direct loopback request."""
    if not _trusted_request(request):
        return Response(status_code=403)
    if await run_in_db(accounts.has_users):
        return Response(content='{"code":"setup_complete"}', status_code=409, media_type="application/json")
    return await run_in_db(_setup_status)


@router.post("/local-download", response_model=None)
async def local_download(request: Request) -> dict | Response:
    """Join the one eligibility-selected local setup flight before first login."""
    if not _trusted_json_post(request):
        return Response(status_code=403)
    if await run_in_db(accounts.has_users):
        return Response(content='{"code":"setup_complete"}', status_code=409, media_type="application/json")
    from llm.local_setup import begin_local_setup

    return await begin_local_setup()


@router.post("/first-account", response_model=None)
async def first_account(request: Request) -> dict | Response:
    """Create exactly one first account and issue the normal browser session."""
    if not _trusted_json_post(request):
        return Response(status_code=403)
    try:
        body = FirstAccountRequest.model_validate(await request.json())
    except (ValidationError, ValueError):
        return Response(status_code=422)
    try:
        user_id = await run_in_db(
            accounts.create_user, body.username, body.password, require_first=True,
        )
    except accounts.FirstAccountExists:
        return Response(content='{"code":"setup_complete"}', status_code=409, media_type="application/json")
    except accounts.AccountError:
        return Response(status_code=422)
    await asyncio.to_thread(_seed_workspace_profile, body.name, body.email or "")
    token, expires_at = await run_in_db(accounts.issue_token, user_id, kind="session", label="browser")
    user = await run_in_db(accounts.get_user, user_id)
    return {
        "token": token,
        "user": user,
        "expires_at": utc_storage_iso_to_api(expires_at),
    }
