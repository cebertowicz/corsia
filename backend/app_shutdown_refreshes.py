"""Refresh-owner shutdown adapters for the FastAPI lifespan."""
from __future__ import annotations


async def shutdown_usage_rollup(logger, run_cleanup_owner):
    """Cancel the single-flight usage rollup before database-pool teardown."""
    try:
        from api.usage_rollup import shutdown_refresh

        return await run_cleanup_owner(
            "usage rollup refresh cleanup", shutdown_refresh(), logger,
        )
    except Exception as exc:
        logger.warning(f"shutdown: usage rollup refresh cleanup failed: {exc}")
        return exc, None


async def shutdown_harness_refreshes(logger, run_cleanup_owner):
    """Join exact harness catalog refresh tasks."""
    try:
        from connections.harness_models import shutdown_refresh_tasks

        return await run_cleanup_owner(
            "harness refresh cleanup", shutdown_refresh_tasks(), logger,
        )
    except Exception as exc:
        logger.warning(f"shutdown: harness refresh cleanup failed: {exc}")
        return exc, None


async def shutdown_refresh_owners(logger, run_cleanup_owner):
    """Join the usage rollup and harness refresh owners in shutdown order."""
    rollup_failure, rollup_cancel = await shutdown_usage_rollup(
        logger, run_cleanup_owner,
    )
    refresh_failure, refresh_cancel = await shutdown_harness_refreshes(
        logger, run_cleanup_owner,
    )
    return (rollup_failure, refresh_failure), rollup_cancel or refresh_cancel
