"""The loopback OTLP/HTTP logs receiver: ``POST /v1/logs`` for declared measure events.

Mounted only on the loopback listener (``infra.otlp_listener``), never on the main app, so the
main app answers 404 for this path. Nothing reads a body until the header checks and the key have
passed, in this order: key shape, restart hold, content type and encoding, content length, key
lookup, request cap. The body is then read outside the single write job slot, which is taken only
to decode and write. Refused requests never write to the database; their counts are kept in
memory (``infra.otlp_state``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from loguru import logger
from starlette.responses import Response

from events.intake import MeasureStamps, admit_event, emitted_event_id
from host_access.credential_sanitizer import sanitize_credential_text
import infra.otlp_admission as admission
import infra.otlp_decode as decode
from infra.database import get_db
from infra.db_async import run_in_db, run_off_db, run_read
from infra.drain import is_draining
from infra.measure_keys import KEY_SHAPE, KeyOwner, resolve
from infra.otlp_state import CAPS, RECORDS_PER_REQUEST, REFUSALS, UNATTRIBUTED
from infra.product_events import emit_product_event

WIRE_LIMIT = 1024 * 1024
INFLATED_LIMIT = 4 * 1024 * 1024
RETRY_AFTER_BUSY = "5"
_TYPES = (decode.PROTOBUF, decode.JSON)
_FIRST_REFUSALS = 3


class _Refused(Exception):
    def __init__(self, status: int, cause: str, text: str, agent: str = UNATTRIBUTED, retry_after: str | None = None) -> None:
        super().__init__(text)
        self.status, self.cause, self.text, self.agent, self.retry_after = status, cause, text, agent, retry_after


def _resolve_key(token: str) -> KeyOwner | None:
    return resolve(get_db(), token)


def _lookup(agent: str, wire: list[decode.WireRecord]) -> admission.Lookup:
    return admission.read_lookup(get_db(), agent, wire)


def _parse(body: bytes, content_type: str) -> tuple[Any, int]:
    request = decode.parse(body, content_type)
    return request, decode.count_records(request)


class OtlpReceiver:
    """The ASGI app of the loopback listener."""

    def __init__(self) -> None:
        self._slot = asyncio.Lock()  # one OTLP write job in flight; telemetry never queues behind itself

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        if scope["path"] != "/v1/logs":
            response = Response("Not found", status_code=404)
        elif scope["method"] != "POST":
            response = Response("Method not allowed", status_code=405, headers={"Allow": "POST"})
        else:
            response = await self._post(scope, receive)
        await response(scope, receive, send)

    async def _post(self, scope: dict, receive: Any) -> Response:
        try:
            return await self._admitted(scope, receive)
        except _Refused as refusal:
            REFUSALS.add(refusal.agent, refusal.cause)
            headers = {"Retry-After": refusal.retry_after} if refusal.retry_after else {}
            return Response(json.dumps({"error": refusal.text}), status_code=refusal.status, headers=headers, media_type="application/json")
        except Exception:  # noqa: BLE001 - one request's failure must not reach the listener
            logger.exception("OTLP receiver failed on a request")
            return Response(json.dumps({"error": "the receiver failed on this request"}), status_code=500, media_type="application/json")

    async def _admitted(self, scope: dict, receive: Any) -> Response:
        headers = {name.decode("latin-1"): value.decode("latin-1") for name, value in scope["headers"]}
        token = _bearer(headers)
        if await run_read(is_draining):
            raise _Refused(503, "draining", "CORSIA is restarting; retry shortly", retry_after=RETRY_AFTER_BUSY)
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in _TYPES or headers.get("content-encoding", "identity").lower() not in ("identity", "gzip"):
            raise _Refused(415, "unsupported", "send application/x-protobuf or application/json, gzip or plain")
        length = headers.get("content-length", "")
        if not length.isdigit():
            raise _Refused(411, "length_required", "a Content-Length header is required")
        if int(length) > WIRE_LIMIT:
            raise _Refused(413, "too_large", "the request body is over 1 MiB")
        owner = await run_read(_resolve_key, token)
        if owner is None:
            raise _Refused(401, "unauthorized", "the key is unknown or revoked")
        agent = owner.agent_thread_id
        if (wait := CAPS.charge_request(agent)) is not None:
            raise _Refused(429, "rate_limited", "too many requests this minute", agent, str(wait))
        # Read the body before taking the slot, so a sender that stalls mid-body holds only its own connection.
        body = await _read_body(receive, int(length), agent)
        if self._slot.locked():
            raise _Refused(503, "busy", "another telemetry request is being written; retry shortly", agent, RETRY_AFTER_BUSY)
        async with self._slot:
            return await self._write(owner, content_type, headers, body)

    async def _write(self, owner: KeyOwner, content_type: str, headers: dict[str, str], body: bytes) -> Response:
        agent = owner.agent_thread_id
        try:
            if headers.get("content-encoding", "").lower() == "gzip":
                body = await run_off_db(decode.inflate, body, INFLATED_LIMIT)
            request, count = await run_off_db(_parse, body, content_type)
        except decode.TooLarge:
            raise _Refused(413, "too_large", "the inflated body is over 4 MiB", agent) from None
        except decode.MalformedRequest as exc:
            raise _Refused(400, "malformed", str(exc), agent) from None
        if count > RECORDS_PER_REQUEST:
            raise _Refused(413, "too_large", f"more than {RECORDS_PER_REQUEST} records in one request; send smaller batches", agent)
        if count and (wait := CAPS.charge_records(agent, count)) is not None:
            raise _Refused(429, "rate_limited", "too many records this minute", agent, str(wait))
        wire = await run_off_db(decode.records, request)
        lookup = await run_read(_lookup, agent, wire)
        workflows = await run_off_db(admission.workflow_stamps, agent, wire)
        items = await run_off_db(admission.prepare, agent, wire, lookup, workflows)
        refused = await run_in_db(admission.write_batch, agent, lookup.project_id, lookup.uses, items)
        for position, item in enumerate(items):
            if item.route and (text := await _route(owner, lookup.project_id, item)) is not None:
                refused[position] = text
        await run_off_db(
            emit_product_event, "otlp_intake", "request_accepted", thread_id=agent,
            properties={"records": count, "rejected": len(refused)},
        )
        texts = [refused[position] for position in sorted(refused)][:_FIRST_REFUSALS]
        message = sanitize_credential_text("; ".join(texts))
        return Response(decode.response_body(content_type, len(refused), message), media_type=content_type)


def _bearer(headers: dict[str, str]) -> str:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or KEY_SHAPE.fullmatch(token.strip()) is None:
        raise _Refused(401, "unauthorized", "send the measure key as Authorization: Bearer <key>")
    return token.strip()


async def _read_body(receive: Any, length: int, agent: str) -> bytes:
    """Read exactly the declared length; a body that runs past it or stops short is malformed."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise _Refused(400, "malformed", "the connection closed before the body ended", agent)
        chunks.append(message.get("body", b""))
        size += len(chunks[-1])
        if size > length:
            raise _Refused(400, "malformed", "the body is longer than its Content-Length", agent)
        if not message.get("more_body"):
            break
    if size != length:
        raise _Refused(400, "malformed", "the body is shorter than its Content-Length", agent)
    return b"".join(chunks)


async def _route(owner: KeyOwner, project_id: str | None, item: admission.Item) -> str | None:
    """Hand a ``route`` or ``both`` record to event intake as an emitter's event; the refusal text, or None."""
    assert item.name is not None and item.attributes is not None
    identity = hashlib.sha256(
        json.dumps([owner.agent_thread_id, item.name, item.ts, item.run_id, item.subject, item.attributes],
                   sort_keys=True, default=str).encode()
    ).hexdigest()
    stamps = MeasureStamps(
        source=admission.SOURCE, provenance=admission.PROVENANCE, run_id=item.run_id,
        workflow_ref=item.workflow_ref, workflow_version=item.workflow_version, project_id=project_id,
        ts=item.ts, subject_key=item.subject,
    )
    result = await admit_event(
        event_type=item.name, facts=item.attributes, origin="emitter", producer=owner.agent_thread_id,
        event_id=emitted_event_id(item.name, identity), stamps=stamps,
    )
    return None if result.refusal is None else f"{item.name}: {result.refusal.text}; the record was dropped."
