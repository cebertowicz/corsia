"""Conductor-only reads of CORSIA's own captured backend log records.

The tool is registered on the shared registry, so it is listed for every
thread; the current thread from ``ToolRegistry`` must be a designated
conductor (workspace or any project) or the call is refused. The response is
bounded to ``MAX_RESPONSE_BYTES`` of UTF-8 JSON and says when it was cut.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from infra.db_async import run_in_db
from infra.otel_logs.capture import SEVERITY_NUMBERS
from infra.otel_logs.lifecycle import capture_status, default_store_path
from infra.otel_logs.store import (
    ReadQuery,
    ReadTimeout,
    StoreUnavailable,
    read_records,
)
from tools.base import Tool

MAX_RESPONSE_BYTES = 16 * 1024
MESSAGE_MAX_CHARS = 1_500
EXCEPTION_MAX_CHARS = 1_500
DEFAULT_WINDOW = timedelta(hours=1)
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
LEVELS = ("INFO", "WARNING", "ERROR", "CRITICAL")


class LogReadError(ValueError):
    """A refusal the model can act on."""


def _parse_time(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LogReadError(f"{name} must be an ISO 8601 time such as 2026-09-28T21:00:00Z") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(ts_ns: int) -> str:
    moment = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _tail(text: str | None, limit: int) -> str | None:
    """Keep an exception's last lines, where the error and innermost frame are."""
    if text is None or len(text) <= limit:
        return text
    return "…" + text[-limit:]


def _head(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " …[cut]"


def _render(row: dict) -> dict:
    where = row["file_path"] or "?"
    if row["line"] is not None:
        where = f"{where}:{row['line']}"
    return {
        "time": _iso(row["ts_ns"]),
        "level": row["severity_text"],
        "logger": row["logger"],
        "message": _head(row["body"], MESSAGE_MAX_CHARS),
        "exception": _tail(row["exception"], EXCEPTION_MAX_CHARS),
        "at": where,
    }


def bounded_response(rows: list[dict], limit: int, window: list[str], status: dict) -> str:
    """Serialize newest-first records under ``MAX_RESPONSE_BYTES``, noting any cut."""
    more_matching = len(rows) > limit
    result: dict[str, Any] = {
        "window": window,
        "records": [],
        "more_matching": more_matching,
        "truncated_for_size": False,
        "capture": status,
    }
    for row in rows[:limit]:
        result["records"].append(_render(row))
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES - 400:
            result["records"].pop()
            result["truncated_for_size"] = True
            break
    records = result["records"]
    if result["truncated_for_size"] or more_matching:
        oldest = records[-1]["time"] if records else window[1]
        result["note"] = (
            f"Showing {len(records)} newest matching records"
            + (" (cut to stay under 16 KiB)" if result["truncated_for_size"] else "")
            + f". For older ones call again with until={oldest}, or narrow the "
            "filters (min_level, logger, contains)."
        )
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _require_conductor(thread_id: str) -> None:
    from infra.conductor import get_conductor_scopes

    if not get_conductor_scopes(thread_id):
        raise LogReadError(
            "not_conductor: log_read is only for a designated conductor "
            "(workspace or project). This is final: do not retry or request a "
            "host lease; ask your conductor to read the logs"
        )


class LogReadTool(Tool):
    """Read CORSIA's own backend log records, newest first."""

    self_describing_errors = True

    @property
    def name(self) -> str:
        return "log_read"

    @property
    def description(self) -> str:
        return (
            "Conductors only. Read CORSIA's own backend log records (the "
            "platform server's warnings, errors and info lines) without a host "
            "lease; other threads get a not_conductor error. Records come "
            "newest first as time (UTC), level, logger, message, exception and "
            "at (file:line). Defaults: the last hour, min_level WARNING, 30 "
            "records. Filter with since/until (UTC ISO 8601), min_level, logger "
            "(a module-name prefix such as core.turn) and contains "
            "(case-insensitive text in the message or exception). The response "
            "stays under 16 KiB: when more_matching or truncated_for_size is "
            "true, follow the note (call again with until set to the oldest "
            "time shown, or narrow the filters). Records are kept about 14 "
            "days. Not captured: DEBUG lines, HTTP access lines, print output, "
            "and lines before capture starts or after it stops; those exist "
            "only in backend.log. capture.state other than on means this "
            "instance is not capturing (test instances are off by design)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "since": {
                    "type": "string",
                    "description": "Inclusive start, UTC ISO 8601. Default: one hour before until.",
                },
                "until": {
                    "type": "string",
                    "description": "Exclusive end, UTC ISO 8601. Default: now.",
                },
                "min_level": {
                    "type": "string", "enum": list(LEVELS),
                    "description": "Lowest level returned. Default WARNING.",
                },
                "logger": {
                    "type": "string", "minLength": 1, "maxLength": 200,
                    "description": "Logger name prefix, e.g. core or infra.database.",
                },
                "contains": {
                    "type": "string", "minLength": 1, "maxLength": 200,
                    "description": "Case-insensitive text in the message or exception.",
                },
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": MAX_LIMIT,
                    "description": f"Most records to return. Default {DEFAULT_LIMIT}.",
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        thread_id = kwargs.get("_thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            return "Error: current thread identity is unavailable; retry from a live turn."
        try:
            until = _parse_time(kwargs.get("until"), "until") or datetime.now(timezone.utc)
            since = _parse_time(kwargs.get("since"), "since") or until - DEFAULT_WINDOW
            if since >= until:
                raise LogReadError("since must be earlier than until")
            level = kwargs.get("min_level") or "WARNING"
            if level not in LEVELS:
                raise LogReadError(f"min_level must be one of {', '.join(LEVELS)}")
            limit = int(kwargs.get("limit") or DEFAULT_LIMIT)
            if not 1 <= limit <= MAX_LIMIT:
                raise LogReadError(f"limit must be 1 to {MAX_LIMIT}")
            await run_in_db(lambda: _require_conductor(thread_id))
            query = ReadQuery(
                since_ns=int(since.timestamp() * 1_000_000_000),
                until_ns=int(until.timestamp() * 1_000_000_000),
                min_severity=SEVERITY_NUMBERS[level],
                logger_prefix=kwargs.get("logger"),
                contains=kwargs.get("contains"),
                limit=limit,
            )
            rows = await run_in_db(lambda: read_records(default_store_path(), query))
        except LogReadError as exc:
            return f"Error: {exc}."
        except StoreUnavailable as exc:
            return (
                f"Error: store_unavailable: {exc}. capture={json.dumps(capture_status())}. "
                "This instance may not capture logs (test instances are off by design)."
            )
        except ReadTimeout:
            return (
                "Error: timeout: the read passed its 2 s deadline. Narrow since/until "
                "or add min_level or logger, then retry once."
            )
        window = [
            since.isoformat(timespec="seconds").replace("+00:00", "Z"),
            until.isoformat(timespec="seconds").replace("+00:00", "Z"),
        ]
        return bounded_response(rows, limit, window, capture_status())
