"""Harness checks that run once when the backend starts."""

from __future__ import annotations

import asyncio

from loguru import logger

from connections.source_state import HARNESS_PROVIDERS, check_harness_source


async def reask_harness_catalogs() -> list[dict]:
    """Re-ask Claude/Codex for their catalogs; a failure is logged, not raised."""
    from connections.harness_refresh import refresh_harness_catalogs

    try:
        results = await refresh_harness_catalogs(reason="startup")
    except Exception as exc:
        logger.warning(f"harness model discovery startup failed: {exc}")
        return []
    failed = [result for result in results if not result.get("ok")]
    if failed:
        logger.warning(f"harness model discovery startup failures: {failed}")
    else:
        logger.info(f"harness model discovery startup: {len(results)} refreshed")
    return results


async def startup_harness_discovery() -> list[dict]:
    """Re-ask both catalogs and re-check both sign-ins, once.

    Claude Code's sign-in is its catalog session's own result, so no second
    session runs for an available row. Codex's ``codex login status`` runs at
    startup in ``seed_builtin_connections`` (before this) and again here, and
    on the recheck route; its later sign-outs wait for one of them.
    """
    outcomes = await asyncio.gather(
        reask_harness_catalogs(),
        *(check_harness_source(provider) for provider in HARNESS_PROVIDERS),
    )
    return outcomes[0]
