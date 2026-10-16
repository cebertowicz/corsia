"""Devices settings and pairing HTTP routes."""

from __future__ import annotations

import base64
import socket

from fastapi import APIRouter, Depends, HTTPException, Request

from core.device_pairing import PairingError, transcript_prefix
from core.devices_runtime import runtime
from infra.auth import require_human_user
from infra.db_async import run_in_db
from infra.devices_listener import host_spki_sha256, local_hostname
from infra.local_connection import is_direct_loopback
from infra.product_events import emit_product_event
from infra.settings import set_devices_enabled


router = APIRouter(prefix="/api/devices", tags=["devices"])


def _management_name() -> str:
    """Return the host name suitable for the local-only management refusal."""
    announcement = runtime.listener.announcement if runtime.listener is not None else None
    if announcement is not None and announcement.announced_name:
        return announcement.announced_name
    return socket.gethostname().rstrip(".").removesuffix(".local")


def _require_local_management(request: Request) -> None:
    if not is_direct_loopback(request):
        raise HTTPException(403, f"Manage connections on {_management_name()} itself")


def _decode(value: object, name: str) -> bytes:
    if not isinstance(value, str):
        raise HTTPException(400, f"{name} must be base64")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as error:
        raise HTTPException(400, f"{name} must be base64") from error


def _emit(action: str, *, reason: str | None = None) -> None:
    properties = {"reason": reason} if reason is not None else None
    emit_product_event("devices", action, properties=properties)


def _pairing_error(error: PairingError) -> HTTPException:
    if error.reason == "too_many_pending":
        return HTTPException(429, {"reason": error.reason})
    if error.reason in {"expired", "too_many_tries", "paused"}:
        return HTTPException(409, {"reason": error.reason})
    if error.reason == "wrong_code":
        return HTTPException(401, {"reason": error.reason})
    return HTTPException(409, {"reason": error.reason})


@router.get("")
async def get_devices(request: Request, _user: dict = Depends(require_human_user)):
    return runtime.snapshot(include_code=is_direct_loopback(request))


@router.put("")
async def put_devices(body: dict, request: Request, _user: dict = Depends(require_human_user)):
    _require_local_management(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(400, "enabled must be a boolean")
    if enabled:
        started = await runtime.enable(request.app)
        if not started:
            return runtime.snapshot()
        runtime.pairing.issue_code()
        _emit("devices_enabled")
        _emit("pairing_code_issued")
    else:
        await runtime.disable()
        _emit("devices_disabled")
    await run_in_db(set_devices_enabled, enabled)
    return runtime.snapshot(include_code=True)


@router.post("/code")
async def issue_code(request: Request, _user: dict = Depends(require_human_user)):
    _require_local_management(request)
    try:
        code = runtime.pairing.issue_code()
    except PairingError as error:
        raise _pairing_error(error) from error
    _emit("pairing_code_issued")
    return {"code": code, **runtime.snapshot(include_code=True)}


@router.post("/pair/start")
async def pair_start(body: dict):
    try:
        # Reject before constructing either transcript: pinned mode would
        # otherwise materialize the host certificate while Devices is off.
        runtime.pairing.require_active()
        mode = body.get("mode")
        prefix = transcript_prefix(
            mode,
            host=body.get("host"),
            spki_sha256=host_spki_sha256() if mode == "pinned" else None,
        )
        exchange_id, msg_s = runtime.pairing.start(
            msg_c=_decode(body.get("msg_c"), "msg_c"),
            transcript_prefix=prefix,
        )
    except PairingError as error:
        _emit("pairing_failed", reason=error.reason)
        if error.reason == "paused":
            _emit("pairing_paused")
        raise _pairing_error(error) from error
    return {"exchange_id": exchange_id, "msg_s": base64.b64encode(msg_s).decode()}


@router.post("/pair/finish")
async def pair_finish(body: dict):
    exchange_id = body.get("exchange_id")
    if not isinstance(exchange_id, str):
        raise HTTPException(400, "exchange_id must be a string")
    try:
        confirm_s, name = runtime.pairing.finish(
            exchange_id=exchange_id,
            confirm_c=_decode(body.get("confirm_c"), "confirm_c"),
            name=local_hostname(),
        )
    except PairingError as error:
        _emit("pairing_failed", reason=error.reason)
        if error.reason == "paused":
            _emit("pairing_paused")
        raise _pairing_error(error) from error
    _emit("pairing_succeeded")
    return {"confirm_s": base64.b64encode(confirm_s).decode(), "name": name}
