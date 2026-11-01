"""Decode an OTLP/HTTP logs request (protobuf or JSON, gzip or not) into flat records, and build its response.

Pure and CPU-bound: the receiver runs it off the event loop and off the database pool. Nothing
here trusts the payload; it only reads the facts the intake design uses (``event.name``, the
time, record attributes, and the ``corsia.*`` resource attributes the server cross-checks).
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from google.protobuf import json_format
from google.protobuf.message import DecodeError as ProtobufDecodeError
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2 as service

PROTOBUF = "application/x-protobuf"
JSON = "application/json"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class MalformedRequest(ValueError):
    """The body is not a valid OTLP logs request."""


class TooLarge(ValueError):
    """The inflated body is over its cap."""


@dataclass(frozen=True)
class WireRecord:
    """One log record: its own attributes, the resource's ``corsia.*`` attributes, and its time."""

    attributes: dict[str, Any]
    resource: dict[str, Any]
    ts: str | None


def inflate(body: bytes, limit: int) -> bytes:
    """Gunzip ``body`` up to ``limit`` inflated bytes; more is :class:`TooLarge`, bad data :class:`MalformedRequest`."""
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = inflater.decompress(body, limit + 1)
    except zlib.error as exc:
        raise MalformedRequest("body is not valid gzip") from exc
    if len(out) > limit or inflater.unconsumed_tail:
        raise TooLarge("inflated body is over the cap")
    return out


def parse(body: bytes, content_type: str) -> service.ExportLogsServiceRequest:
    request = service.ExportLogsServiceRequest()
    try:
        if content_type == PROTOBUF:
            request.ParseFromString(body)
        else:
            json_format.Parse(body, request, ignore_unknown_fields=True)
    except (ProtobufDecodeError, json_format.ParseError, UnicodeDecodeError, RecursionError) as exc:
        raise MalformedRequest("body is not a valid OTLP logs request") from exc
    return request


def count_records(request: service.ExportLogsServiceRequest) -> int:
    return sum(len(scope.log_records) for resource in request.resource_logs for scope in resource.scope_logs)


def _value(value: Any) -> Any:
    kind = value.WhichOneof("value")
    if kind is None:
        return None  # an empty value is an absent attribute
    if kind in ("string_value", "bool_value", "int_value", "double_value"):
        return getattr(value, kind)
    # An array, map or bytes value fits no declared kind, so it is kept in its OTLP JSON form:
    # admission then drops and notes it, or refuses a stamp sent in that shape, instead of losing it here.
    return json_format.MessageToDict(value)


def _flat(pairs: Any, only_corsia: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        if only_corsia and not pair.key.startswith("corsia."):
            continue
        value = _value(pair.value)
        if value is not None:
            out[pair.key] = value
    return out


def _time(nanos: int) -> str | None:
    # A uint64 of nanoseconds ends in 2554, inside datetime's range, so this can't overflow.
    if not nanos:
        return None
    return (_EPOCH + timedelta(microseconds=nanos // 1000)).isoformat()


def records(request: service.ExportLogsServiceRequest) -> list[WireRecord]:
    found: list[WireRecord] = []
    for resource in request.resource_logs:
        shared = _flat(resource.resource.attributes, only_corsia=True)
        for scope in resource.scope_logs:
            for record in scope.log_records:
                # OTLP: a zero event time means unknown; the SDK's observed time stands in for it.
                when = record.time_unix_nano or record.observed_time_unix_nano
                found.append(WireRecord(_flat(record.attributes), shared, _time(when)))
    return found


def response_body(content_type: str, rejected: int, message: str) -> bytes:
    """The OTLP response for a request that was processed: ``partial_success`` only when something was rejected."""
    reply = service.ExportLogsServiceResponse()
    if rejected:
        reply.partial_success.rejected_log_records = rejected
        reply.partial_success.error_message = message
    if content_type == PROTOBUF:
        return reply.SerializeToString()
    return json_format.MessageToJson(reply, preserving_proto_field_name=False).encode()
