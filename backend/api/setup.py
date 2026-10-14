"""Loopback-only, pre-auth first-run setup.

These routes deliberately expose only the facts needed before an account can
exist. They are not a general unauthenticated provider or account API.
"""

from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, ValidationError

from infra import accounts, config
from infra.db_async import run_in_db
from infra.default_port import DEFAULT_BACKEND_PORT
from infra.local_connection import is_direct_loopback
from infra.time import utc_storage_iso_to_api

router = APIRouter(prefix="/api/setup", tags=["setup"])

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_TAURI_ORIGINS = frozenset({"tauri:" + "//localhost", "http:" + "//tauri.localhost"})


class FirstAccountRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


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


def _ram_tier(ram_bytes: int) -> int:
    """Return the highest local-profile RAM threshold this Mac reaches."""
    from llm.local_profiles import PROFILES
    from llm.local_setup_gates import GIB

    return max(profile.min_ram_gib for profile in PROFILES if ram_bytes >= profile.min_ram_gib * GIB)


def _app_location_class() -> str:
    """Expose a coarse package class, never the executable's path."""
    executable = Path(sys.executable)
    return "application_bundle" if ".app" in executable.parts else "other"


def _setup_status() -> dict:
    """Build the bounded pre-auth screen facts without exposing machine identity."""
    from llm.local_setup_gates import host_ram_bytes, profile_eligibility
    from llm.local_profiles import DEFAULT_PROFILE

    ram_bytes = host_ram_bytes()
    compatible = platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}
    try:
        free_disk_bytes = shutil.disk_usage(config.STATE_ROOT).free
    except OSError:
        free_disk_bytes = None
    return {
        "macos_apple_silicon": compatible,
        "ram_tier_gib": _ram_tier(ram_bytes),
        "free_disk_bytes": free_disk_bytes,
        "app_location": _app_location_class(),
        "local_eligible": compatible and profile_eligibility(DEFAULT_PROFILE, ram_bytes).eligible,
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
    token, expires_at = await run_in_db(accounts.issue_token, user_id, kind="session", label="browser")
    user = await run_in_db(accounts.get_user, user_id)
    return {
        "token": token,
        "user": user,
        "expires_at": utc_storage_iso_to_api(expires_at),
    }
