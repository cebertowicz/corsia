"""Request a safe boundary from the active provider turn."""

from infra.db_async import run_in_db


async def request_active_boundary(thread_id: str) -> None:
    """Ask the active provider turn to yield at a safe boundary."""
    from infra.database import get_db
    from infra.send_service import request_safe_boundary
    from llm.resolution import resolve_execution_target

    connection = get_db()
    try:
        row = connection.execute(
            "SELECT model FROM active_turns WHERE thread_id=? AND status='running'",
            (thread_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return
    target = await run_in_db(resolve_execution_target, str(row["model"]))
    await request_safe_boundary(thread_id, target.harness)
