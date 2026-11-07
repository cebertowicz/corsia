"""Close model-owned browsers before activating host access."""

import asyncio

from loguru import logger

from host_access.failure_reasons import HostAccessBrowserIsolationError
from host_access.refusal_messages import exception_chain_summary
from infra.db_async import run_in_db


async def _close_user_model_browsers(user_id: str) -> None:
    """Prove all model-owned browser sessions are closed before prompting."""

    def session_ids() -> tuple[str, ...]:
        from infra.database import get_db

        db = get_db()
        try:
            rows = db.execute(
                "SELECT id FROM threads WHERE user_id=? ORDER BY id",
                (user_id,),
            ).fetchall()
            return tuple(str(row["id"]) for row in rows)
        finally:
            db.close()

    from host_access.processes import PROCESS_OWNER

    owned = await run_in_db(session_ids)
    results = await asyncio.gather(
        *(PROCESS_OWNER.close_browser(thread_id) for thread_id in owned),
        return_exceptions=True,
    )
    failure = next(
        (result for result in results if isinstance(result, BaseException)),
        None,
    )
    if failure is not None:
        logger.warning(
            "host-access browser isolation failed for user {}: {}",
            user_id, exception_chain_summary(failure),
        )
        raise HostAccessBrowserIsolationError(
            "model browser isolation could not be established"
        ) from failure
