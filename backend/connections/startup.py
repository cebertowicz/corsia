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
    """Refresh account catalogs and record source checks without ambient login probes.

    Claude's source check uses the catalog result, so startup does not launch
    a second session for it. Codex's source check runs alongside the refresh.
    """
    async def catalogs_then_claude() -> list[dict]:
        from connections.models import get_connection
        from connections.source_state import apply_check, refresh_check
        from infra.db_async import run_in_db

        results = await reask_harness_catalogs()
        result = next((item for item in results if item.get("provider") == "claude-code"), None)
        row = await run_in_db(get_connection, "claude-code")
        if result is None:
            await check_harness_source("claude-code")
        elif row and row["status"] != "not_installed":
            await run_in_db(apply_check, "claude-code", *refresh_check(result))
        return results

    outcomes = await asyncio.gather(
        catalogs_then_claude(),
        *(check_harness_source(provider) for provider in HARNESS_PROVIDERS
          if provider != "claude-code"),
    )
    return outcomes[0]
