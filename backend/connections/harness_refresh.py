"""When Claude Code and Codex model catalogs are re-asked.

``harness_models`` owns discovery, the catalog rows, and the refresh tasks.
This module owns the triggers. Request paths (the model list, the thread
list, thread create, model switch) never wait on a version probe or a stale
catalog: they serve the current rows and start a background check. Only a
selector the catalog has never seen waits, because it cannot be validated
otherwise.

The background check spawns ``--version`` only when the CLI binary itself
changed. An update replaces the file, so its resolved path, inode, size or
mtime moves; an unchanged file reuses the version it reported last time.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Iterable
from typing import Any

import connections.harness_models as harness_models
from connections.harness_models import RefreshReason
from infra.database import get_db, write_transaction

Fingerprint = tuple[str, int, int, int]

# provider -> (fingerprint of the binary probed, the version it reported).
_PROBED: dict[str, tuple[Fingerprint, str]] = {}
# provider -> selectors requested while its check was already running.
_PENDING: dict[str, set[str]] = {}


def _fingerprint(binary: str) -> Fingerprint | None:
    path = shutil.which(binary)
    if path is None:
        return None
    try:
        real = os.path.realpath(path)
        stat = os.stat(real)
    except OSError:
        return None
    return real, stat.st_ino, stat.st_size, stat.st_mtime_ns


async def _harness_version(
    provider: str, binary: str, *, forced: bool,
) -> str | None:
    """The binary's version, probed only when the file changed or on demand."""
    fingerprint = _fingerprint(binary)
    probed = _PROBED.get(provider)
    if not forced and fingerprint is not None and probed and probed[0] == fingerprint:
        return probed[1]
    version = await harness_models._version_or_none(binary)
    if fingerprint is not None and version is not None:
        _PROBED[provider] = (fingerprint, version)
    return version


def _notify_models_changed() -> None:
    from infra.state.ws import notify_models_changed

    notify_models_changed()


def _selection(model: str | None) -> tuple[str, str] | None:
    if not isinstance(model, str) or "/" not in model:
        return None
    provider, selector = model.split("/", 1)
    known = provider in harness_models._ADAPTERS
    return (provider, selector) if known and selector else None


def _known_selectors(provider: str, *, db=None) -> set[str]:
    connection = db or get_db()
    close = db is None
    try:
        return {
            row["provider_model_id"]
            for row in connection.execute(
                "SELECT provider_model_id FROM models WHERE harness = ?",
                (provider,),
            ).fetchall()
        }
    finally:
        if close:
            connection.close()


def _refresh_state(
    provider: str, version: str | None, *, db=None,
) -> tuple[dict[str, Any], RefreshReason | None, set[str]]:
    connection = db or get_db()
    close = db is None
    try:
        cache = harness_models.read_discovery_envelope(provider, db=connection) or {}
        reason = harness_models.cache_refresh_reason(
            provider, harness_version=version, db=connection,
        )
        return cache, reason, _known_selectors(provider, db=connection)
    finally:
        if close:
            connection.close()


def _record_checked_unknown(
    provider: str, requested: set[str], *, db=None,
) -> None:
    with write_transaction(db, scope="harness_model_catalog") as connection:
        current = harness_models.read_discovery_envelope(provider, db=connection) or {}
        unknown = requested - _known_selectors(provider, db=connection)
        if unknown:
            current["checked_unknown_selectors"] = sorted(
                set(current.get("checked_unknown_selectors") or []) | unknown
            )
            harness_models._save_discovery_envelope(provider, current, db=connection)


def _group(models: Iterable[str | None]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for model in models:
        selected = _selection(model)
        if selected:
            grouped.setdefault(selected[0], set()).add(selected[1])
    return grouped


async def _refresh_if_needed(
    provider: str, *, selectors: set[str] | None = None, db=None,
    forced: RefreshReason | None = None,
) -> dict[str, Any] | None:
    binary, unavailable = await harness_models._db_call(
        harness_models._provider_binary, provider, db=db,
    )
    if unavailable:
        return None
    assert binary is not None
    version = await _harness_version(provider, binary, forced=forced is not None)
    requested = selectors or set()
    cache, reason, known = await harness_models._db_call(
        _refresh_state, provider, version, db=db,
    )
    reason = forced or reason
    if reason is None and not cache.get("last_error"):
        checked = set(cache.get("checked_unknown_selectors") or [])
        if requested - known - checked:
            reason = "unknown_selection"
    if reason is None:
        return None
    result = await harness_models.refresh_provider(
        provider, reason=reason, harness_version=version, db=db,
    )
    if result.get("ok") and requested:
        await harness_models._db_call(
            _record_checked_unknown, provider, requested, db=db,
        )
    if result.get("ok"):
        # An open picker reloads now instead of on its next open.
        _notify_models_changed()
    return result


async def refresh_for_selections(models, *, db=None) -> list[dict[str, Any]]:
    """Batch loaded/requested selectors to one version probe per provider."""
    results = await asyncio.gather(*(
        _refresh_if_needed(provider, selectors=selectors, db=db)
        for provider, selectors in _group(models).items()
    ))
    return [result for result in results if result is not None]


async def refresh_for_selection(model: str | None, *, db=None) -> dict | None:
    """Refresh one requested selector without rewriting or fallback."""
    results = await refresh_for_selections([model], db=db)
    return results[0] if results else None


async def refresh_harness_catalogs(*, reason: RefreshReason) -> list[dict[str, Any]]:
    """Re-ask available harnesses whatever the cache age: releases skip CLI bumps."""
    results = await asyncio.gather(*(
        _refresh_if_needed(provider, forced=reason)
        for provider in harness_models._ADAPTERS
    ))
    return [result for result in results if result is not None]


async def _drain(provider: str) -> None:
    """One check, then one more for any selectors that arrived during it."""
    while True:
        await _refresh_if_needed(provider, selectors=_PENDING.pop(provider, set()))
        # No await between this test and returning, so a request cannot add
        # selectors after it and still find this check running.
        if not _PENDING.get(provider):
            return


def _schedule(provider: str, selectors: set[str] | None = None) -> None:
    if selectors:
        _PENDING.setdefault(provider, set()).update(selectors)
    harness_models.start_background_check(
        provider, lambda provider=provider: _drain(provider),
    )


def schedule_stale_refresh() -> None:
    """Check every harness catalog in the background; the caller never waits."""
    for provider in harness_models._ADAPTERS:
        _schedule(provider)


def schedule_refresh_for_selections(models: Iterable[str | None]) -> None:
    """Check the providers behind these selections in the background.

    A check already running for the provider takes these selectors on its
    next pass rather than dropping them.
    """
    for provider, selectors in _group(models).items():
        _schedule(provider, selectors)


async def prepare_selection(model: str | None) -> None:
    """Admit a requested selector: known ones never wait, unknown ones do."""
    selected = _selection(model)
    if selected is None:
        return
    provider, selector = selected
    if selector in await harness_models._db_call(_known_selectors, provider):
        schedule_refresh_for_selections([model])
        return
    await refresh_for_selection(model)
