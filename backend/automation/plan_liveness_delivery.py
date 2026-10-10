"""Delivery and runtime-idle adapters for structured plan liveness."""

from infra.database import get_db


def thread_is_idle(thread_id: str, *, db=None) -> bool:
    """Return whether durable state has no active turn for the thread."""

    connection = db or get_db()
    try:
        row = connection.execute(
            """SELECT 1 FROM active_turns
               WHERE thread_id=? AND status='running'
               LIMIT 1""",
            (thread_id,),
        ).fetchone()
        return row is None
    finally:
        if db is None:
            connection.close()


async def notify_plan_liveness(
    deliver,
    thread_id: str,
    internal_event: str,
    summary: str,
    details: dict,
) -> bool:
    """Deliver one event while keeping a failed wake retryable."""

    from loguru import logger

    try:
        delivered = await deliver(thread_id, internal_event, summary, details)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "plan-liveness: {} delivery for {} failed: {}",
            internal_event,
            thread_id,
            exc,
        )
        return False
    if not delivered:
        logger.warning(
            "plan-liveness: {} delivery for {} returned false",
            internal_event,
            thread_id,
        )
        return False
    return True
