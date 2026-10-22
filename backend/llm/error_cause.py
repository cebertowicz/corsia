"""The one table from a provider failure to its machine ``cause_kind``.

Error metadata (``llm_error`` on the native path, ``cc_error`` and Claude Code
``result`` rows on the harness path) carries a ``cause_kind`` the UI turns into
one sentence. It is always one of :data:`KINDS`; provider text never goes in
it. The classifiers that decide each cause already exist (auth, overflow,
stream idle, usage limits, HTTP status); this module only names their outputs.

Native: a failed ``LLMResponse`` carries an ``error_kind`` code, set at the
dispatch seams (:func:`native_error_kind` for the codes without an earlier
owner). Harness: a row's existing ``cause`` plus, where the cause alone does not
say it, the terminal error text read by the same classifiers.
"""

from __future__ import annotations

from typing import Any

KINDS: tuple[str, ...] = (
    "sign_in_expired",
    "auth_rejected",
    "usage_limit",
    "rate_limited",
    "server_unreachable",
    "model_not_loaded",
    "context_overflow",
    "stream_stalled",
    "transcript_rejected",
    "process_exited",
    "backend_shutdown",
    "unknown",
)

AUTH_REJECTED = "provider_auth_rejected"
USAGE_LIMIT = "provider_usage_limit"
RATE_LIMITED = "provider_rate_limited"
SERVER_UNREACHABLE = "provider_server_unreachable"
MODEL_NOT_LOADED = "provider_model_not_loaded"

#: Native ``LLMResponse.error_kind`` codes and harness ``cause`` values.
_KIND_OF_CODE = {
    AUTH_REJECTED: "auth_rejected",
    USAGE_LIMIT: "usage_limit",
    RATE_LIMITED: "rate_limited",
    SERVER_UNREACHABLE: "server_unreachable",
    MODEL_NOT_LOADED: "model_not_loaded",
    "provider_context_overflow": "context_overflow",
    "provider_stream_stalled": "stream_stalled",
    "provider_stream_truncated": "stream_stalled",
    "sign_in_expired": "sign_in_expired",
    "context_window_exceeded": "context_overflow",
    "provider_transcript_poisoned": "transcript_rejected",
    "process_exited": "process_exited",
    "backend_shutdown": "backend_shutdown",
    "usageLimitExceeded": "usage_limit",
}

#: Causes that name themselves and are not re-read from the error text.
_SELF_NAMING = frozenset({
    "context_window_exceeded", "provider_transcript_poisoned", "backend_shutdown",
})


def cause_kind(code: object) -> str:
    """The kind for a native ``error_kind`` or a harness ``cause``; ``unknown`` otherwise."""
    return _KIND_OF_CODE.get(code, "unknown") if isinstance(code, str) else "unknown"


def native_error_kind(error: BaseException) -> str | None:
    """The ``error_kind`` code for a native failure the stream and overflow seams did not name.

    Decided by HTTP status and exception type, plus the shared usage-limit
    classifier; no other provider wording is read.
    """
    from llm.providers.omlx_lifecycle import OmlxLoadedStateError
    from llm.server_observation import endpoint_failure_of, http_status_of
    from subagents.provider_usage_limits import is_provider_usage_or_quota_limit

    if isinstance(error, OmlxLoadedStateError):
        return MODEL_NOT_LOADED
    if endpoint_failure_of(error) == "auth":
        return AUTH_REJECTED
    if is_provider_usage_or_quota_limit(error):
        return USAGE_LIMIT
    if http_status_of(error) in (429, 529):
        return RATE_LIMITED
    return SERVER_UNREACHABLE if _is_connection_failure(error) else None


def _is_connection_failure(error: BaseException) -> bool:
    """Whether the chain holds a failure to connect, by exception type.

    Only the transport's own connect errors count. The SDK wrappers
    (``APIConnectionError`` and its ``APITimeoutError`` subclass, litellm's
    catch-all) also wrap read timeouts and unrelated failures from a server
    that answered, so they are matched only through what they wrap.
    """
    import httpx

    from llm.server_observation import exception_chain

    kinds = (httpx.ConnectError, httpx.ConnectTimeout, ConnectionRefusedError)
    return any(isinstance(current, kinds) for current in exception_chain(error))


async def mark_own_server(response: Any, omlx_server_id: str | None) -> Any:
    """Flag a failed oMLX response whose server is CORSIA's own (``own_server``).

    Only the kinds whose remedy depends on it (unreachable, not loaded) are
    flagged; the server is the one local_sources already names, read off the
    event loop and only on those failures. Returns ``response``.
    """
    if omlx_server_id and response.error_kind in (SERVER_UNREACHABLE, MODEL_NOT_LOADED):
        from infra.db_async import run_read
        from llm.local_sources import corsia_omlx_server

        own = await run_read(corsia_omlx_server)
        response.own_server = own is not None and own["id"] == omlx_server_id
    return response


def response_error_fields(response: Any) -> dict[str, Any]:
    """The error step payload fields of a failed native response."""
    fields: dict[str, Any] = {"cause_kind": cause_kind(response.error_kind)}
    if response.resets_at:
        fields["resets_at"] = response.resets_at
    if response.own_server:
        fields["own_server"] = True
    return fields


def error_fields(payload: dict) -> dict[str, Any]:
    """``cause_kind`` (always one of :data:`KINDS`), ``resets_at`` and ``own_server`` of an error step payload."""
    kind = payload.get("cause_kind")
    fields: dict[str, Any] = {"cause_kind": kind if kind in KINDS else "unknown"}
    for key in ("resets_at", "own_server"):
        if payload.get(key):
            fields[key] = payload[key]
    return fields


def reset_iso(detail: object, reset_at: object = None) -> str | None:
    """The strict future ISO reset time of an error already classified as a usage limit, else None."""
    from subagents.provider_usage_limits import provider_reset_deadline

    deadline = provider_reset_deadline(detail, reset_at=reset_at, known_limit=True)
    return deadline.isoformat() if deadline else None


def harness_cause_kind(harness: str, cause: str | None, detail: Any = None) -> str:
    """The kind of one harness failure row.

    ``cause`` is the row's own cause. A process exit is an expired login only
    when its stderr says so; where a cause does not name the failure (a result
    row, an unmapped Codex value) the error text decides the expired login and
    the usage limit, through their existing classifiers.
    """
    if cause in _SELF_NAMING:
        return cause_kind(cause)
    from connections.auth_failure import auth_failure_kind
    from subagents.provider_usage_limits import is_provider_usage_or_quota_limit

    if auth_failure_kind(harness, detail):
        return "sign_in_expired"
    if cause == "process_exited":
        return "process_exited"
    if cause == "usageLimitExceeded" or (
        detail is not None and is_provider_usage_or_quota_limit(detail)
    ):
        return "usage_limit"
    return cause_kind(cause)


def cause_fields(
    harness: str, cause: str | None, detail: Any = None, reset_at: object = None,
    model: str | None = None,
) -> dict[str, str]:
    """Row metadata for one harness failure: ``cause``, ``cause_kind``, and ``resets_at`` / ``model`` when known.

    ``cause`` is omitted when None so a row never gains a null cause.
    """
    kind = harness_cause_kind(harness, cause, detail)
    fields = {"cause": cause} if cause is not None else {}
    fields["cause_kind"] = kind
    if kind == "usage_limit" and (resets := reset_iso(detail or "", reset_at)):
        fields["resets_at"] = resets
    if model:
        fields["model"] = model
    return fields


def codex_failure_meta(
    cause: str, message: str | None, reset_at: str | None, model: str | None, usage: dict,
) -> dict[str, Any]:
    """The ``cc_error`` metadata of a failed Codex turn; Codex's ``reset_at`` is stored only as ``resets_at``."""
    return {"codex": True, "type": "cc_error", "event_type": "harness.error",
            "harness": "codex", **cause_fields("codex", cause, message, reset_at, model), **usage}
