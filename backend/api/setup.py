"""Loopback-only, pre-auth first-run setup.

These routes deliberately expose only the facts needed before an account can
exist. They are not a general unauthenticated provider or account API. The one
account route, ``/first-account``, hands the request to
``accounts.create_first_account`` (the same owner ``corsia user add`` uses for
the first account); it exists only while no account does.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from api.setup_requests import FirstAccountRequest, LocalDownloadRequest, SetupProgressUpdate
from core.corrections_asset_download import FLIGHT as CLASSIFIER_DOWNLOAD
from core.corrections_asset_download import public_state as classifier_state
from infra import accounts, config, setup_progress
from infra.auth import authenticate, require_user, token_from_request
from infra.db_async import run_in_db
from infra.local_connection import is_direct_loopback
from infra.time import utc_storage_iso_to_api
from llm.local_setup_foreign import apply_foreign_omlx_choice, foreign_omlx_answer
from llm.local_setup_results import setup_fix

router = APIRouter(prefix="/api/setup", tags=["setup"])
logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_TAURI_ORIGINS = frozenset({"tauri:" + "//localhost", "http:" + "//tauri.localhost"})
_MINIMUM_MACOS_MAJOR = 15


def _own_backend_port(request: Request) -> int | None:
    """Resolve the one port this backend process is serving, never a caller's value."""
    server = request.scope.get("server")
    if server and isinstance(server[1], int):
        return server[1]
    configured = os.environ.get("BACKEND_PORT", "")
    return int(configured) if configured.isdigit() and 1 <= int(configured) <= 65535 else None


def _loopback_host(request: Request) -> bool:
    """Require a literal loopback Host for this backend process's bound port."""
    own_port = _own_backend_port(request)
    host = request.headers.get("host")
    if own_port is None or host is None:
        return False
    name, separator, port = host.rpartition(":")
    return bool(separator) and name in _LOOPBACK_HOSTS and port == str(own_port)


def _trusted_request(request: Request) -> bool:
    """Require a direct local peer, literal loopback target, and local origin."""
    if not (is_direct_loopback(request) and _loopback_host(request)):
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    own_port = _own_backend_port(request)
    if own_port is None:
        return False
    return origin in {
        *("http:" + f"//{host}:{own_port}" for host in _LOOPBACK_HOSTS),
        *_TAURI_ORIGINS,
    }


def _trusted_json_post(request: Request) -> bool:
    """Apply the pre-auth POST transport boundary without reading its body."""
    if not _trusted_request(request):
        return False
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/json":
        return False
    if request.headers.get("origin") is None:
        # Browsers attach Sec-Fetch-* metadata; a missing Origin is reserved
        # for a direct non-browser client that has already passed Host + JSON.
        return not any(name in request.headers for name in ("sec-fetch-mode", "sec-fetch-site"))
    return True


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
    if "AppTranslocation" in parts:
        return "disk_image"
    if len(parts) > 1 and parts[1] == "Volumes":
        return "disk_image"
    try:
        app_index = parts.index("CORSIA.app")
    except ValueError:
        return "other"
    return "applications" if app_index > 0 and parts[app_index - 1] == "Applications" else "other"


def _work_folder_display(workspace: Path) -> str:
    """Return a home-abbreviated workspace label with no account name leakage."""
    try:
        relative = workspace.resolve().relative_to(Path.home().resolve())
    except ValueError:
        return f"…/{workspace.name}" if workspace.name else "…"
    return "~" if not relative.parts else f"~/{relative.as_posix()}"


def _required_username() -> str | None:
    """The one data owner with no account, whose name the first account must take."""
    owners = accounts.data_owners_without_accounts()
    return owners[0] if len(owners) == 1 else None


def _ready_ai() -> str | None:
    """The name of a CLI source that the startup check already found Ready, else None.

    Only the stored check result is read, so nothing runs before an account exists. A
    stale "ready" is harmless: the account step rechecks with a session before it skips.
    """
    from connections.account_profiles import profile_for
    from connections.models import get_connection
    from connections.source_state import HARNESS_PROVIDERS, row_state

    for provider in HARNESS_PROVIDERS:
        if row_state(get_connection(provider))["state"] == "ready":
            return profile_for(provider).label
    return None


def _ai_check_pending() -> bool:
    """Whether the startup check has not yet recorded a result for Claude Code or Codex.

    Welcome waits briefly on this before it asks which subscription the person has, so a
    tool that is already installed is found first rather than asked about.
    """
    from connections.models import get_connection
    from connections.source_state import HARNESS_PROVIDERS, row_state

    return any(row_state(get_connection(provider))["checked_at"] is None for provider in HARNESS_PROVIDERS)


def _setup_status() -> dict:
    """Build the bounded pre-auth screen facts without exposing machine identity."""
    from llm.local_setup_gates import local_memory_ok

    apple_silicon = platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}
    macos_supported = _macos_supported()
    # Only the 24 GiB gate: which model fits is the managed oMLX's pick (GET /local-model).
    memory_ok = local_memory_ok()
    try:
        free_disk_gb = round(shutil.disk_usage(config.STATE_ROOT).free / 1000**3, 1)
    except OSError:
        free_disk_gb = None
    return {
        "apple_silicon": apple_silicon,
        "macos_supported": macos_supported,
        "ram_tier": "eligible" if memory_ok else "below_minimum",
        "free_disk_gb": free_disk_gb,
        "app_location": _app_location_class(),
        "local_eligible": apple_silicon and macos_supported and memory_ok,
        "work_folder": _work_folder_display(config.WORKSPACE),
        "required_username": _required_username(),
        "classifier": classifier_state(CLASSIFIER_DOWNLOAD.status()),
        "ready_ai": _ready_ai(),
        "ai_check_pending": _ai_check_pending(),
        "other_omlx": foreign_omlx_answer(),
    }


@router.get("/status", response_model=None)
async def setup_status(request: Request) -> dict | Response:
    """Report only first-run screen facts to a direct loopback request."""
    if not _trusted_request(request):
        return Response(status_code=403)
    if await run_in_db(accounts.has_users):
        return Response(content='{"code":"setup_complete"}', status_code=409, media_type="application/json")
    return await run_in_db(_setup_status)


@router.get("/progress", response_model=None)
async def setup_progress_view(_user: dict = Depends(require_user)) -> dict:
    """Where setup stands for a signed-in person; while unfinished, the eligibility facts are read fresh."""

    def _read() -> dict:
        progress = setup_progress.view()
        return progress if progress["setup_completed"] else {**progress, "status": _setup_status()}

    return await run_in_db(_read)


@router.put("/progress", response_model=None)
async def setup_progress_update(request: Request, authorization: str | None = Header(None)) -> dict | Response:
    """Record a setup step, the local-AI choice, Welcome's picks or completion.

    Before the first account only the local-AI choice and Welcome's picks are taken, on the same loopback boundary as the
    other pre-account routes; after it, any signed-in session may record all of them.
    """
    signed_in = await run_in_db(accounts.has_users)
    if signed_in:
        if not authenticate(token_from_request(None, authorization)):
            return Response(status_code=401)
    elif not _trusted_json_post(request):
        return Response(status_code=403)
    try:
        body = SetupProgressUpdate.model_validate(await request.json())
    except (ValidationError, ValueError):
        return Response(status_code=422)
    if not signed_in and (body.step is not None or body.handoff is not None or body.completed):
        return Response(status_code=422)
    try:
        await run_in_db(lambda: setup_progress.update(
            step=body.step, local_ai=body.local_ai, picks=body.picks,
            handoff=body.handoff.model_dump(exclude_none=True) if body.handoff is not None else None,
            completed=body.completed))
    except setup_progress.SetupProgressError:
        return Response(status_code=422)
    return {"ok": True}


async def _before_first_account(request: Request, *, post: bool) -> Response | None:
    """The pre-auth refusal for this request, else None."""
    if not (_trusted_json_post(request) if post else _trusted_request(request)):
        return Response(status_code=403)
    if await run_in_db(accounts.has_users):
        return Response(content='{"code":"setup_complete"}', status_code=409, media_type="application/json")
    return None


@router.get("/local-model", response_model=None)
async def local_model_pick(request: Request) -> dict | Response:
    """Welcome's local row: checking, the model CORSIA's oMLX picked, or why there is none."""
    if refused := await _before_first_account(request, post=False):
        return refused
    from llm.local_setup_prepare import pick_view

    return await pick_view()


@router.post("/local-model", response_model=None)
async def local_model_prepare(request: Request) -> dict | Response:
    """Start the pick when Welcome opens with the switch on, or Try again after it failed.

    Answers once the pick settles: nothing can push it to Welcome before the first account.
    """
    if refused := await _before_first_account(request, post=True):
        return refused
    from llm.local_setup_prepare import begin_prepare, settled_view

    await begin_prepare()
    return await settled_view()


@router.post("/local-model/stop", response_model=None)
async def local_model_stop(request: Request) -> dict | Response:
    """The switch went off: stop the pick and the oMLX it started; nothing was downloaded."""
    if refused := await _before_first_account(request, post=True):
        return refused
    from llm.local_setup_prepare import stop_prepare

    return await stop_prepare()


@router.get("/local-download", response_model=None)
async def local_download_status(request: Request) -> dict | Response:
    """The safe, live setup-flight state for Welcome's pre-account progress rail."""
    if refused := await _before_first_account(request, post=False):
        return refused
    from onboarding.local_download_state import current

    return {**current(), "other_omlx": foreign_omlx_answer()}


@router.post("/local-download", response_model=None)
async def local_download(request: Request) -> dict | Response:
    """Join the one local setup flight before first login; it downloads only the pick."""
    if not _trusted_json_post(request):
        return Response(status_code=403)
    if await run_in_db(accounts.has_users):
        return Response(content='{"code":"setup_complete"}', status_code=409, media_type="application/json")
    try:
        body = LocalDownloadRequest.model_validate(await request.json())
    except (ValidationError, ValueError):
        return Response(status_code=422)
    if refusal := await asyncio.to_thread(
        apply_foreign_omlx_choice,
        other_omlx=body.other_omlx,
    ):
        return JSONResponse(status_code=409, content={"code": refusal, "fix": setup_fix("other_omlx")})
    from llm.local_setup_flow import begin_local_setup

    return await begin_local_setup(onboarding=True)


@router.post("/local-download/stop", response_model=None)
async def stop_local_download(request: Request) -> dict | Response:
    """Stop the pre-account setup flight under the same local JSON boundary that starts it."""
    if not _trusted_json_post(request):
        return Response(status_code=403)
    if await run_in_db(accounts.has_users):
        return Response(content='{"code":"setup_complete"}', status_code=409, media_type="application/json")
    from llm.local_setup import cancel_local_setup

    return await cancel_local_setup()


@router.get("/classifier", response_model=None)
async def classifier_status(request: Request) -> dict | Response:
    """The required corrections classifier's download state; also read after the account exists."""
    if not _trusted_request(request):
        return Response(status_code=403)
    return classifier_state(CLASSIFIER_DOWNLOAD.status())


@router.post("/classifier", response_model=None)
async def classifier_download(request: Request) -> dict | Response:
    """Start, join or retry the one classifier flight. Takes no path or URL from the caller."""
    if not _trusted_json_post(request):
        return Response(status_code=403)
    return classifier_state(CLASSIFIER_DOWNLOAD.start())


@router.post("/classifier/recheck", response_model=None)
async def classifier_recheck(request: Request) -> dict | Response:
    """The setup window regained focus: resume a download stopped for space if there is room now."""
    if not _trusted_json_post(request):
        return Response(status_code=403)
    return classifier_state(CLASSIFIER_DOWNLOAD.recheck_space())


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
            accounts.create_first_account, body.username, body.password,
            call_name=body.name, email=body.email or "", first_run_path="app",
        )
    except accounts.FirstAccountExists:
        return Response(content='{"code":"setup_complete"}', status_code=409, media_type="application/json")
    except accounts.AccountError as error:
        return JSONResponse(status_code=422, content={"detail": str(error)})
    except Exception:
        # No user field is in this message; the traceback names only the failing step.
        logger.exception("First-account creation failed")
        return JSONResponse(
            status_code=500,
            content={"code": "setup_failed", "detail": "Your account was not created. Try again."},
        )
    token, expires_at = await run_in_db(accounts.issue_token, user_id, kind="session", label="browser")
    user = await run_in_db(accounts.get_user, user_id)
    return {
        "token": token,
        "user": user,
        "expires_at": utc_storage_iso_to_api(expires_at),
    }
