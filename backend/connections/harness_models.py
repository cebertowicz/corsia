"""Claude Code and Codex model discovery into the canonical model catalog.

``models`` owns model identity and availability. ``connections.config`` keeps
only the refresh envelope (version, timestamps, and last error). A refresh
never rewrites a saved selection or chooses a fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

from connections.claude_discovery import (
    ClaudeCatalogCheckFailed, _discover_claude_context, discover_claude_models,
)
from connections.claude_unlisted import add_accepted_unlisted
from connections.codex_discovery import discover_codex_models
from connections.harness_legacy_fallback import remove_legacy_fallback
from connections.harness_model_rows import (
    CACHE_KEY,
    CLAUDE_SOURCE,
    CODEX_SOURCE,
    normalize_claude_models,
    normalize_codex_models,
)
from connections.harness_model_rows import VERSION as _VERSION, _positive_int
from connections.harness_process import killed_on_exit
from connections.models import get_connection
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db

CACHE_MAX_AGE = timedelta(hours=1)
CACHE_FAILURE_RETRY_AGE = timedelta(minutes=5)
RefreshReason = Literal[
    "startup", "missing", "version_changed", "retry_after_failure",
    "missing_context_evidence", "stale", "unknown_selection", "manual", "account-signin", "account-verify", "usage-recheck",
]
_REFRESH_TASKS: dict[str | tuple, asyncio.Task[Any]] = {}
# Off-request-path checks (version probe, then refresh if needed), one per provider.
_CHECK_TASKS: dict[str, asyncio.Task[Any]] = {}
log = logging.getLogger(__name__)


async def _db_call(fn, *args, db=None, **kwargs):
    """Keep caller-owned connections on their creating thread."""
    if db is not None:
        return fn(*args, db=db, **kwargs)
    return await run_in_db(fn, *args, **kwargs)


def read_discovery_envelope(provider: str, *, db=None) -> dict[str, Any] | None:
    """Return connector refresh metadata; model rows never live here."""
    connection = get_connection(provider, db=db)
    cache = ((connection or {}).get("config") or {}).get(CACHE_KEY)
    if not isinstance(cache, dict):
        return None
    envelope = dict(cache)
    envelope.pop("models", None)
    return envelope


def _save_discovery_envelope(
    provider: str, envelope: dict[str, Any], *, db
) -> None:
    row = db.execute(
        "SELECT config FROM connections WHERE provider = ?", (provider,)
    ).fetchone()
    if not row:
        raise ValueError(f"connection not found: {provider}")
    config = json.loads(row["config"]) if row["config"] else {}
    if not isinstance(config, dict):
        raise ValueError(f"connection config must be an object: {provider}")
    clean = dict(envelope)
    clean.pop("models", None)
    config[CACHE_KEY] = clean
    db.execute(
        "UPDATE connections SET config = ?, updated_at = ? WHERE provider = ?",
        (json.dumps(config, sort_keys=True), datetime.now(timezone.utc).isoformat(), provider),
    )


def _catalog_rows(provider: str, *, db) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.execute(
            "SELECT * FROM models WHERE harness = ? ORDER BY id", (provider,)
        ).fetchall()
    ]


def _prior_selectors(provider: str, *, db=None) -> list[dict[str, str]]:
    """Discovered catalog rows, for re-probing selectors the harness stops listing."""
    connection = db or get_db()
    close = db is None
    try:
        rows = _catalog_rows(provider, db=connection)
    finally:
        if close:
            connection.close()
    return [
        {
            "selector": str(row["provider_model_id"]),
            "resolved_identity": str(_metadata(row).get("resolved_identity") or ""),
            "display_name": str(row.get("display_name") or ""),
        }
        for row in rows
        if not _metadata(row).get("reference_only")
    ]


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row.get("metadata_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def record_runtime_context_observation(
    provider: str,
    *,
    selector: str,
    reported_identity: str,
    effective_tokens: int,
    source: str,
    observed_at: str | None = None,
    db=None,
) -> int | None:
    """Correct one exact catalog row from provider runtime telemetry.

    The update is identity-bound: both the transport selector and the
    provider-reported model must match the row. Runtime telemetry is already
    the provider's effective window, so discovered percentages remain debug
    provenance and are never applied to it again.
    """
    if provider not in _ADAPTERS:
        raise ValueError(f"unsupported harness provider: {provider}")
    exact_selector = str(selector or "").strip()
    exact_reported = str(reported_identity or "").strip()
    window = _positive_int(effective_tokens)
    if not exact_selector or not exact_reported or window is None:
        return None
    with write_transaction(db, scope="runtime_context_observation") as connection:
        rows = connection.execute(
            "SELECT id,context_window,metadata_json FROM models "
            "WHERE harness = ? AND provider_model_id = ?",
            (provider, exact_selector),
        ).fetchall()
        if len(rows) != 1:
            return None
        row = dict(rows[0])
        metadata = _metadata(row)
        if str(metadata.get("resolved_identity") or "") != exact_reported:
            return None
        prior = metadata.get("context_observation")
        debug_raw_tokens = window
        debug_source = None
        percent = 100
        max_tokens = window
        if provider == "codex" and isinstance(prior, dict):
            debug_raw_tokens = _positive_int(prior.get("raw_tokens")) or window
            percent = _positive_int(prior.get("effective_percent")) or 100
            percent = min(percent, 100)
            max_tokens = _positive_int(prior.get("max_tokens")) or window
            debug_source = str(
                prior.get("debug_source") or prior.get("source") or ""
            ) or None
        observation = {
            "source": str(source),
            "version": str(metadata.get("harness_version") or "unknown"),
            "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
            "raw_tokens": debug_raw_tokens,
            "max_tokens": max_tokens,
            "effective_tokens": window,
            "effective_percent": percent,
        }
        if debug_source is not None:
            observation["debug_source"] = debug_source
        metadata["context_observation"] = observation
        timestamp = observation["observed_at"]
        connection.execute(
            "UPDATE models SET context_window = ?, metadata_json = ?, "
            "updated_at = ? WHERE id = ?",
            (window, json.dumps(metadata, sort_keys=True), timestamp, row["id"]),
        )
        return window


def _retire_unlisted(
    connection, prior, current, timestamp: str, errors: dict[str, str],
) -> None:
    """Mark rows discovery no longer returns unavailable, with the probe's reason."""
    retained = sorted(
        set(prior) - set(current),
        key=lambda selector: (
            _metadata(prior[selector]).get("discovery_order", 2147483647),
            selector,
        ),
    )
    for retained_order, selector in enumerate(retained, start=len(current)):
        row = prior[selector]
        meta = _metadata(row)
        if meta.get("reference_only"):
            continue
        meta["availability_reason"] = "not_returned_by_discovery"
        meta["discovery_order"] = retained_order
        connection.execute(
            "UPDATE models SET available = 0, last_error = ?, "
            "metadata_json = ?, updated_at = ? WHERE id = ?",
            (errors.get(selector), json.dumps(meta, sort_keys=True), timestamp, row["id"]),
        )


def store_success(
    provider: str, rows: list[dict[str, Any]], *, harness_version: str,
    source: str, timestamp: str, db=None, unlisted_errors: dict[str, str] | None = None,
) -> None:
    """Atomically persist discovery rows and the connection refresh envelope.

    ``unlisted_errors`` names why each previously known, now unlisted selector
    was not kept; it becomes that row's ``last_error``.
    """
    if provider not in _ADAPTERS:
        raise ValueError(f"unsupported harness provider: {provider}")
    current: dict[str, tuple[int, dict[str, Any]]] = {}
    for order, row in enumerate(rows):
        selector = str(row.get("selector_id") or "").strip()
        if not selector or "/" in selector:
            raise ValueError(f"invalid {provider} selector: {selector!r}")
        if selector in current:
            raise ValueError(f"duplicate {provider} selector: {selector}")
        current[selector] = (order, dict(row))

    with write_transaction(db, scope="harness_model_catalog") as connection:
        remove_legacy_fallback(provider, db=connection)
        prior_rows = _catalog_rows(provider, db=connection)
        prior = {str(row["provider_model_id"]): row for row in prior_rows}
        for selector in current:
            model_id = f"{provider}/{selector}"
            by_id = connection.execute(
                "SELECT id,harness,provider,provider_model_id FROM models WHERE id = ?",
                (model_id,),
            ).fetchone()
            by_key = connection.execute(
                "SELECT id,harness,provider,provider_model_id FROM models "
                "WHERE provider = ? AND provider_model_id = ?",
                (provider, selector),
            ).fetchone()
            for existing in (by_id, by_key):
                if existing and (
                    existing["id"] != model_id
                    or existing["harness"] != provider
                    or existing["provider"] != provider
                    or existing["provider_model_id"] != selector
                ):
                    raise ValueError(f"harness model collision: {model_id}")

        _retire_unlisted(connection, prior, current, timestamp, unlisted_errors or {})

        backend = "cc" if provider == "claude-code" else "codex"
        for selector, (order, row) in current.items():
            model_id = f"{provider}/{selector}"
            old = prior.get(selector)
            meta = _metadata(old) if old else {}
            resolved_identity = str(row.get("resolved_identity") or "")
            same_resolved_identity = bool(
                old
                and resolved_identity
                and str(meta.get("resolved_identity") or "")
                == resolved_identity
            )
            if not same_resolved_identity:
                meta.pop("context_observation", None)
            meta.pop("reference_only", None)
            meta.pop("availability_reason", None)
            alias_of = row.get("alias_of")
            # An accepted-but-unlisted selector keeps running for whatever is
            # pinned to it; the picker offers only the listed name it aliases.
            if alias_of:
                meta["alias_of"] = f"{provider}/{alias_of}"
            else:
                meta.pop("alias_of", None)
            meta.update({
                "resolved_identity": row.get("resolved_identity"),
                "discovered_capabilities": row.get("capabilities") or {},
                "is_default": bool(row.get("is_default")),
                "harness_version": harness_version,
                "discovery_order": order,
            })
            observation = row.get("context_observation")
            if isinstance(observation, dict):
                meta["context_observation"] = observation
            context_window = row.get("context_window")
            if context_window is None and old is not None and same_resolved_identity:
                # A failed per-model probe does not erase a prior exact
                # discovery/runtime observation.
                context_window = old.get("context_window")
            available = row.get("available") is True
            if not available:
                meta["availability_reason"] = (
                    row.get("availability_reason") or "discovered_model_unavailable"
                )
            capabilities = row.get("capabilities") or {}
            supports_vision = "image" in (capabilities.get("input_modalities") or [])
            supports_reasoning = bool(
                capabilities.get("effort_levels")
                or capabilities.get("reasoning_efforts")
            )
            connection.execute(
                "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
                "display_name,description,backend,source,enabled,available,context_window,"
                "supports_vision,supports_tools,supports_reasoning,metadata_json,"
                "default_temperature,default_max_tokens,last_error,discovered_at,"
                "last_seen_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "harness=excluded.harness,provider=excluded.provider,"
                "provider_model_id=excluded.provider_model_id,"
                "litellm_model=excluded.litellm_model,"
                "display_name=excluded.display_name,description=excluded.description,"
                "backend=excluded.backend,source=excluded.source,available=excluded.available,"
                "enabled=MAX(models.enabled,models.source='reference'),"  # keeps an operator hide
                "context_window=excluded.context_window,supports_vision=excluded.supports_vision,"
                "supports_tools=1,supports_reasoning=excluded.supports_reasoning,"
                "default_temperature=NULL,default_max_tokens=NULL,"
                "metadata_json=excluded.metadata_json,last_error=NULL,"
                "last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at",
                (
                    model_id, provider, provider, selector, model_id,
                    str(row.get("display_name") or selector),
                    str(row.get("description") or ""), backend, source,
                    1, int(available), context_window,
                    int(supports_vision), 1, int(supports_reasoning),
                    json.dumps(meta, sort_keys=True),
                    None, timestamp, timestamp, timestamp, timestamp,
                ),
            )
        previous = read_discovery_envelope(provider, db=connection) or {}
        _save_discovery_envelope(provider, {
            **previous,
            "harness_version": harness_version,
            "source": source,
            "refreshed_at": timestamp,
            "attempted_at": timestamp,
            "last_error": None,
        }, db=connection)


def store_failure(
    provider: str, error: str, *, harness_version: str,
    source: str, timestamp: str, db=None,
) -> None:
    """Record failed refresh without replacing last-known model rows."""
    with write_transaction(db, scope="harness_model_catalog") as connection:
        remove_legacy_fallback(provider, db=connection)
        previous = read_discovery_envelope(provider, db=connection) or {}
        _save_discovery_envelope(provider, {
            **previous,
            "harness_version": harness_version,
            "source": source,
            "attempted_at": timestamp,
            "last_error": error,
        }, db=connection)


def _parse_time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def cache_refresh_reason(
    provider: str, *, harness_version: str | None = None,
    now: datetime | None = None, db=None,
) -> RefreshReason | None:
    """Return why an event-triggered refresh is needed, or ``None``."""
    cache = read_discovery_envelope(provider, db=db)
    if not cache:
        return "missing"
    if harness_version and cache.get("harness_version") != harness_version:
        return "version_changed"
    current = now or datetime.now(timezone.utc)
    attempted = _parse_time(cache.get("attempted_at"))
    # Only a full failure uses the short retry age; evidence waits for staleness.
    backoff = attempted is not None and current - attempted < CACHE_FAILURE_RETRY_AGE
    if cache.get("last_error"):
        return None if backoff else "retry_after_failure"
    connection = db or get_db()
    close = db is None
    try:
        missing_context = connection.execute(
            "SELECT 1 FROM models WHERE harness = ? AND enabled = 1 "
            "AND source != 'legacy_fallback' AND context_window IS NULL LIMIT 1",
            (provider,),
        ).fetchone()
    finally:
        if close:
            connection.close()
    refreshed = _parse_time(cache.get("refreshed_at"))
    if missing_context:
        return None if refreshed and current - refreshed <= CACHE_MAX_AGE else "missing_context_evidence"
    if refreshed is None or current - refreshed > CACHE_MAX_AGE:
        return "stale"
    return None


async def binary_version(binary: str, *, env=None) -> str:
    """Read a harness version without starting a model turn."""
    process = await asyncio.create_subprocess_exec(
        binary, "--version", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **({'env': env, 'stdin': asyncio.subprocess.DEVNULL} if env is not None else {}),
    )
    try:
        async with killed_on_exit(process):
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except asyncio.TimeoutError:
        raise RuntimeError(f"{binary} --version timed out") from None
    match = _VERSION.search(stdout.decode("utf-8", "replace"))
    if process.returncode or not match:
        detail = stderr.decode("utf-8", "replace").strip() or "invalid output"
        raise RuntimeError(f"{binary} --version failed: {detail}")
    return match.group(0)


_ADAPTERS = {
    "claude-code": (discover_claude_models, normalize_claude_models, CLAUDE_SOURCE),
    "codex": (discover_codex_models, normalize_codex_models, CODEX_SOURCE),
}


async def _version_or_none(binary: str) -> str | None:
    try:
        return await binary_version(binary)
    except (OSError, RuntimeError):
        return None


def _provider_binary(provider: str, *, db=None) -> tuple[str | None, str | None]:
    connection = get_connection(provider, db=db)
    if not connection:
        return None, "connection_not_found"
    if connection.get("status") != "available":
        return None, "connection_unavailable"
    binary = str((connection.get("config") or {}).get("binary") or provider)
    return binary, None


def _cached_harness_version(provider: str, *, db=None) -> str:
    cache = read_discovery_envelope(provider, db=db) or {}
    return str(cache.get("harness_version") or "unknown")


async def _refresh_provider_once(
    provider: str, *, reason: RefreshReason, harness_version: str | None = None, db=None,
    binary: str | None = None, context=None,
) -> dict[str, Any]:
    # A caller that names the binary asks even a row that is not available (a sign-in check).
    binary, unavailable = (binary, None) if binary else await _db_call(
        _provider_binary, provider, db=db)
    if unavailable:
        return {"provider": provider, "ok": False, "reason": reason, "error": unavailable}
    assert binary is not None
    if context is not None:
        from connections.account_discovery_refresh import refresh_account_once

        return await refresh_account_once(provider, context=context, binary=binary,
            harness_version=harness_version, adapter=_ADAPTERS[provider], db_call=_db_call,
            version_read=binary_version, db=db)
    discover, normalize, source = _ADAPTERS[provider]
    timestamp = datetime.now(timezone.utc).isoformat()
    version = harness_version
    try:
        version = version or await binary_version(binary)
        payload = await discover(binary)
        unlisted_errors: dict[str, str] = {}
        if provider == "claude-code":
            prior = await _db_call(_prior_selectors, provider, db=db)
            unlisted_errors = await add_accepted_unlisted(
                payload, prior,
                lambda selector: _discover_claude_context(binary, selector),
            )
        rows = normalize(payload, harness_version=version, timestamp=timestamp)
        await _db_call(
            store_success,
            provider, rows, harness_version=version, source=source,
            timestamp=timestamp, db=db, unlisted_errors=unlisted_errors,
        )
        return {"provider": provider, "ok": True, "reason": reason,
                "model_count": len(rows), "harness_version": version}
    except Exception as exc:
        version = version or await _db_call(
            _cached_harness_version, provider, db=db,
        )
        error = str(exc)
        await _db_call(
            store_failure,
            provider, error, harness_version=version, source=source,
            timestamp=timestamp, db=db,
        )
        return {"provider": provider, "ok": False, "reason": reason,
                "error": error, "harness_version": version,
                "source_check_recorded": isinstance(exc, ClaudeCatalogCheckFailed)}


async def refresh_provider(
    provider: str, *, reason: RefreshReason, harness_version: str | None = None, db=None,
    binary: str | None = None, account_id: str | None = None, expected_receipt=None,
) -> dict[str, Any]:
    """Single-flight by account generation; unscoped Default retains its provider key."""
    context = None
    key = provider
    if account_id is not None:
        from connections.account_discovery_types import capture_discovery_context, AccountRefreshResult

        context = await _db_call(capture_discovery_context, provider, account_id=account_id,
                                expected_receipt=expected_receipt, db=db)
        if context is None:
            return AccountRefreshResult(provider, account_id, cause='account_refresh_superseded')
        if context.is_default:
            context = None  # Default's pre/post observation is handled by its canonical path.
        else:
            key = context.key
    task = _REFRESH_TASKS.get(key)
    if task is None or task.done():
        task = asyncio.create_task(_refresh_provider_once(
            provider, reason=reason, harness_version=harness_version, db=db, binary=binary,
            **({'context': context} if context is not None else {}),
        ))
        _REFRESH_TASKS[key] = task

        def _clear(done: asyncio.Task, *, key=key) -> None:
            if _REFRESH_TASKS.get(key) is done:
                _REFRESH_TASKS.pop(key, None)

        task.add_done_callback(_clear)
    result = await asyncio.shield(task)
    if context is not None:
        return result
    return {**result, "reason": reason}


def start_background_check(provider: str, check) -> None:
    """Run ``check()`` off the request path unless one is already running."""
    running = _CHECK_TASKS.get(provider)
    if running is not None and not running.done():
        return
    task = asyncio.create_task(check())
    _CHECK_TASKS[provider] = task

    def _settle(done: asyncio.Task, *, key: str = provider) -> None:
        if _CHECK_TASKS.get(key) is done:
            _CHECK_TASKS.pop(key, None)
        if not done.cancelled() and done.exception() is not None:
            log.warning("harness background check failed for %s: %s",
                        key, done.exception())

    task.add_done_callback(_settle)


async def shutdown_refresh_tasks() -> None:
    """Cancel and join the exact in-process harness checks and refreshes."""
    refreshes = tuple(
        (cast(dict[str | tuple, asyncio.Task[Any]], registry), provider, task)
        for registry in (_CHECK_TASKS, _REFRESH_TASKS)
        for provider, task in registry.items()
    )
    for _registry, _provider, task in refreshes:
        if not task.done():
            task.cancel()
    join = asyncio.gather(
        *(task for _registry, _provider, task in refreshes),
        return_exceptions=True,
    )
    pending_cancel: asyncio.CancelledError | None = None
    while not join.done():
        try:
            await asyncio.shield(join)
        except asyncio.CancelledError as exc:
            pending_cancel = pending_cancel or exc
    results = join.result()
    for (registry, provider, task), result in zip(refreshes, results, strict=True):
        if registry.get(provider) is task:
            registry.pop(provider, None)
        if isinstance(result, BaseException) and not isinstance(
            result, asyncio.CancelledError,
        ):
            log.warning("harness refresh shutdown failed for %s: %s", provider, result)
    if pending_cancel is not None:
        raise pending_cancel
    if _CHECK_TASKS or _REFRESH_TASKS:
        raise RuntimeError(
            "harness refresh tasks remain: "
            f"{tuple(_CHECK_TASKS) + tuple(_REFRESH_TASKS)}"
        )
