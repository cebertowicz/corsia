"""Devices settings and pairing HTTP routes."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import re

from fastapi import APIRouter, Depends, HTTPException, Request

from core.device_pairing import PairingError, transcript_prefix
from core.devices_runtime import runtime
from infra.auth import require_human_user
from infra.db_async import run_in_db
from infra.devices_events import emit_devices_event, emit_pairing_failure
from infra.devices_identity import host_spki_der, host_spki_sha256, sign_identity_proof
from infra.local_connection import is_direct_loopback
from infra.settings import get_devices_outside_address, set_devices_enabled, set_devices_outside_address


router = APIRouter(prefix="/api/devices", tags=["devices"])


def _management_name() -> str:
    """Return the canonical name suitable for the local-only management refusal."""
    return runtime.remote_connect_name()


def _require_local_management(request: Request) -> None:
    if not is_direct_loopback(request):
        raise HTTPException(403, f"Manage connections on {_management_name()} itself")


def _direct_snapshot() -> dict:
    """The direct-only management routes always return an editable host snapshot."""
    return {**runtime.snapshot(include_code=True), "read_only": False}


def _decode(value: object, name: str) -> bytes:
    if not isinstance(value, str):
        raise HTTPException(400, f"{name} must be base64")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as error:
        raise HTTPException(400, f"{name} must be base64") from error


def _outside_address(value: object) -> str | None:
    """Validate the host setting once at its backend boundary."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(400, "outside_address must be a string or null")
    text = value.strip()
    if not text:
        return None
    if re.match(r"^http://", text, re.IGNORECASE):
        raise HTTPException(400, "Type the outside address without http://, like home.example.com.")
    text = re.sub(r"^https://", "", text, flags=re.IGNORECASE).removesuffix("/")
    message = "Type a name, like home.example.com, or a name and port, like home.example.com:8443."
    endpoint = re.fullmatch(r"([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?", text)
    if endpoint is None:
        raise HTTPException(400, message)
    host, port = endpoint.groups()
    labels = host.removesuffix(".").split(".")
    if len(host) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
        raise HTTPException(400, message)
    # URL clients interpret a numeric final label as an IPv4 address. Refuse
    # malformed numeric hosts here rather than saving a route they cannot use.
    if re.fullmatch(r"(?:[0-9]+|0[xX][0-9A-Fa-f]+)", labels[-1]):
        try:
            ipaddress.IPv4Address(host.removesuffix("."))
        except ValueError:
            raise HTTPException(400, message) from None
    if port is not None and not 1 <= int(port) <= 65535:
        raise HTTPException(400, message)
    return f"{host}:{int(port)}" if port is not None else host


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
    direct_loopback = is_direct_loopback(request)
    return {**runtime.snapshot(include_code=direct_loopback), "read_only": not direct_loopback}


@router.put("")
async def put_devices(body: dict, request: Request, _user: dict = Depends(require_human_user)):
    _require_local_management(request)
    if "outside_address" in body:
        address = _outside_address(body["outside_address"])
        await run_in_db(set_devices_outside_address, address)
        runtime.outside_address = address
        emit_devices_event("outside_address_saved" if address else "outside_address_cleared")
        return _direct_snapshot()
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(400, "enabled must be a boolean")
    if enabled:
        was_enabled = runtime.enabled
        started = await runtime.enable(request.app)
        if not started:
            return _direct_snapshot()
        if not was_enabled:
            runtime.pairing.issue_code()
            emit_devices_event("devices_enabled")
            emit_devices_event("pairing_code_issued")
    else:
        await runtime.disable()
        emit_devices_event("devices_disabled")
    await run_in_db(set_devices_enabled, enabled)
    return _direct_snapshot()


@router.post("/code")
async def issue_code(request: Request, _user: dict = Depends(require_human_user)):
    _require_local_management(request)
    try:
        code = runtime.pairing.issue_code()
    except PairingError as error:
        raise _pairing_error(error) from error
    emit_devices_event("pairing_code_issued")
    return {**_direct_snapshot(), "code": code}


@router.post("/pair/start")
async def pair_start(body: dict):
    try:
        # Reject before constructing either transcript: pinned mode would
        # otherwise materialize the host certificate while Devices is off.
        runtime.pairing.require_active()
        mode = body.get("mode")
        try:
            prefix = transcript_prefix(
                mode,
                host=body.get("host"),
                spki_sha256=host_spki_sha256() if mode == "pinned" else None,
            )
        except ValueError:
            raise runtime.pairing.reject_malformed() from None
        msg_c = _decode(body.get("msg_c"), "msg_c")
        spake, msg_s, generation = await asyncio.to_thread(runtime.pairing.start_spake)
        exchange_id, msg_s = runtime.pairing.start(
            msg_c=msg_c,
            transcript_prefix=prefix,
            spake=spake,
            msg_s=msg_s,
            generation=generation,
        )
    except PairingError as error:
        emit_pairing_failure(reason=error.reason)
        if error.reason == "paused":
            emit_devices_event("pairing_paused")
        raise _pairing_error(error) from error
    return {"exchange_id": exchange_id, "msg_s": base64.b64encode(msg_s).decode()}


@router.get("/outside-address")
async def outside_address():
    """The saved outside address is public because it is the hostname a person types."""
    return {"outside_address": await run_in_db(get_devices_outside_address)}


@router.post("/pair/finish")
async def pair_finish(body: dict):
    exchange_id = body.get("exchange_id")
    if not isinstance(exchange_id, str):
        raise HTTPException(400, "exchange_id must be a string")
    try:
        identity = host_spki_sha256()
        confirm_s, name, confirm_identity = runtime.pairing.finish(
            exchange_id=exchange_id,
            confirm_c=_decode(body.get("confirm_c"), "confirm_c"),
            name=runtime.remote_connect_name(),
            identity=identity,
        )
    except PairingError as error:
        emit_pairing_failure(reason=error.reason)
        if error.reason == "paused":
            emit_devices_event("pairing_paused")
        raise _pairing_error(error) from error
    emit_devices_event("pairing_succeeded")
    result = {
        "confirm_s": base64.b64encode(confirm_s).decode(),
        "name": name,
        "outside_address": await run_in_db(get_devices_outside_address),
    }
    result["spki_sha256"] = base64.b64encode(identity).decode()
    result["confirm_identity"] = base64.b64encode(confirm_identity).decode()
    return result


@router.post("/identity")
async def identity(body: dict):
    """Prove that a system-trusted route reaches this host's stable key."""
    encoded_nonce = body.get("nonce")
    # A 32-byte nonce is exactly 44 base64 characters; refusing anything else first bounds the decode on a public route.
    if not isinstance(encoded_nonce, str) or len(encoded_nonce) != 44:
        raise HTTPException(400, "identity proof requires a 32-byte nonce and authority")
    nonce = _decode(encoded_nonce, "nonce")
    authority = body.get("authority")
    if len(nonce) != 32 or not isinstance(authority, str) or not authority or len(authority) > 255:
        raise HTTPException(400, "identity proof requires a 32-byte nonce and authority")
    if authority != authority.lower() or any(ord(char) > 127 for char in authority):
        raise HTTPException(400, "identity authority must be lower-case ASCII")
    return {
        "spki": base64.b64encode(host_spki_der()).decode(),
        "signature": base64.b64encode(sign_identity_proof(nonce, authority)).decode(),
    }
