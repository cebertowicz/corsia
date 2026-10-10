"""Storage teardown after canonical runtime owners have all joined."""
from __future__ import annotations

from app_shutdown_product_events import flush_product_events
from app_startup_runtime import stop_runtime_logs
from doceditor.live_runtime import close_documents


def _shutdown_storage(logger, shutdown_pool) -> Exception | None:
    """Attempt both storage owners and return the first close failure."""
    from infra.db_async import shutdown as shutdown_db_async

    failure = None
    try:
        shutdown_db_async()
    except Exception as exc:
        failure = exc
        logger.warning(f"shutdown: db_async cleanup failed: {exc}")
    try:
        shutdown_pool()
    except Exception as exc:
        failure = failure or exc
        logger.warning(f"shutdown: database pool cleanup failed: {exc}")
    return failure


async def finish_storage_shutdown(logger, pending_cancel, *, run_cleanup_owner, shutdown_pool) -> None:
    """Keep document publication alive through turn joins, then finish storage."""
    document_failure, document_cancel = await run_cleanup_owner(
        "documents", close_documents(), logger,
    )
    pending_cancel = pending_cancel or document_cancel
    flush_product_events(logger)
    stop_runtime_logs(logger)
    storage_failure = _shutdown_storage(logger, shutdown_pool)
    if pending_cancel is not None:
        raise pending_cancel
    if document_failure is not None:
        raise document_failure
    if storage_failure is not None:
        raise storage_failure
