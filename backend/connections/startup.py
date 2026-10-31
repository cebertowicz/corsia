"""Harness checks that run once when the backend starts."""

from __future__ import annotations

from loguru import logger


async def reask_harness_catalogs() -> list[dict]:
    """Re-ask Claude/Codex for every signed-in account's catalog; a failure is logged, not raised.

    Each account's refresh records its own discovery, which is where the vendor's
    sign-in state is read from (``source_state.harness_state``).
    """
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
