"""oMLX schema introspection for server-accepted sampling fields."""

from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx
from loguru import logger


_CACHE_TTL_SECONDS = 3600.0
_FETCH_TIMEOUT = 3.0

_CACHE: dict[tuple[str, int], tuple[float, frozenset[str]]] = {}

_CANONICAL_SAMPLING_FIELDS: frozenset[str] = frozenset({
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "max_tokens",
    "repetition_penalty",
    "repetition_context_size",
    "frequency_penalty",
    "presence_penalty",
    "thinking_budget",
    "seed",
})

_FALLBACK_KNOWN_DROPPED: frozenset[str] = frozenset({
    "top_k",
    "repetition_penalty",
    "repetition_context_size",
})

_ADMIN_HONORED_FIELDS: frozenset[str] = frozenset({
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "max_tokens",
    "repetition_penalty",
    "presence_penalty",
})


def accepted_fields_cached(api_base: str) -> frozenset[str] | None:
    """Return cached wire-accepted sampling fields, or None if cold/stale."""
    host, port = _parse_host_port(api_base)
    if host is None or port is None:
        return None
    entry = _CACHE.get((host, port))
    if entry is None:
        return None
    cached_at, fields = entry
    if (time.monotonic() - cached_at) > _CACHE_TTL_SECONDS:
        return None
    return fields


def silently_dropped_cached(api_base: str) -> frozenset[str]:
    """Return sampling fields this server drops and CORSIA cannot compensate for."""
    accepted = accepted_fields_cached(api_base)
    if accepted is not None:
        return (_CANONICAL_SAMPLING_FIELDS - accepted) - _ADMIN_HONORED_FIELDS
    logger.warning(
        f"oMLX schema not introspected yet for {api_base!r} — "
        "falling back to static silently-dropped map "
        f"({sorted(_FALLBACK_KNOWN_DROPPED - _ADMIN_HONORED_FIELDS)}). "
        "Schema may differ from oMLX 0.3.8 baseline."
    )
    return _FALLBACK_KNOWN_DROPPED - _ADMIN_HONORED_FIELDS


async def warm_cache(
    api_base: str, *, server_id: str | None = None,
) -> frozenset[str] | None:
    """Fetch ``/openapi.json`` and populate the cache for this server."""
    host, port = _parse_host_port(api_base)
    if host is None or port is None:
        logger.warning(
            f"oMLX introspection: cannot parse host/port from {api_base!r}"
        )
        return None

    cached = accepted_fields_cached(api_base)
    if cached is not None:
        return cached

    url = f"http://{host}:{port}/openapi.json"
    try:
        headers = {}
        if server_id:
            from llm.providers.omlx_public_auth import public_headers
            headers = await public_headers({"server_id": server_id}, url=url)
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(
            f"oMLX introspection failed for {host}:{port}: {e!r} "
            "(callers will fall back to static silently-dropped map)"
        )
        return None

    fields = _extract_chat_completion_fields(data)
    if fields is None:
        logger.warning(
            f"oMLX introspection: {host}:{port} returned an openapi.json "
            "without a ChatCompletionRequest schema — falling back."
        )
        return None

    _CACHE[(host, port)] = (time.monotonic(), fields)
    dropped = _CANONICAL_SAMPLING_FIELDS - fields
    logger.info(
        f"oMLX introspection: {host}:{port} accepts "
        f"{len(fields)} request fields; "
        f"silently-dropped sampling knobs: {sorted(dropped) or 'none'}"
    )
    return fields


def invalidate_cache(api_base: str | None = None) -> None:
    """Drop cached schema. ``None`` clears all entries."""
    if api_base is None:
        _CACHE.clear()
        return
    host, port = _parse_host_port(api_base)
    if host is None or port is None:
        return
    _CACHE.pop((host, port), None)


def _parse_host_port(api_base: str) -> tuple[str | None, int | None]:
    try:
        u = urlparse(api_base)
        return u.hostname, u.port
    except Exception:
        return None, None


def _extract_chat_completion_fields(openapi: dict) -> frozenset[str] | None:
    """Pull property names from ``components.schemas.ChatCompletionRequest``."""
    schemas = (openapi.get("components") or {}).get("schemas") or {}
    ccr = schemas.get("ChatCompletionRequest") or {}
    props = ccr.get("properties") or {}
    if not props:
        return None
    return frozenset(props.keys())
