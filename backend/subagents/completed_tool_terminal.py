"""Stateless terminal helpers for results retained after an effect completed."""

from __future__ import annotations

from tools.recorded_call import CompletedToolFailure


def exit_detail(
    error: str | BaseException,
    code: int | None,
    process_name: str,
    boundary_violated: bool,
) -> str:
    """Return only safe process detail; retained result bytes stay typed."""
    raw_detail = str(error) if isinstance(error, BaseException) else error
    detail = (raw_detail or "").strip() or (
        f"{process_name} exited with code {code} before the turn terminal."
    )
    return f"{process_name} capability boundary violated" if boundary_violated else detail


def retained_error(error: str | BaseException, detail: str) -> str | BaseException:
    """Keep a completed-result failure typed for the existing terminal owner."""
    return error if isinstance(error, CompletedToolFailure) else detail


async def persist_completed_terminal_results(
    turn_id: str,
    error: str | BaseException,
    *,
    db=None,
) -> tuple[object, ...]:
    """Persist retained bytes through the existing exact-Turn writer."""
    if not isinstance(error, CompletedToolFailure):
        return ()
    from infra.completed_tool_results import persist_completed_tool_results

    if db is not None:
        return persist_completed_tool_results(turn_id, error, db=db)
    from infra.db_async import run_in_db

    return await run_in_db(persist_completed_tool_results, turn_id, error)


async def settle_codex_results(
    thread_id: str,
    live,
    identity,
    error: str | BaseException | None,
    bcast,
    db=None,
) -> None:
    """Persist and settle retained Codex results before generic tool debt."""
    if not isinstance(error, CompletedToolFailure):
        return
    completed = await persist_completed_terminal_results(identity.turn_id, error, db=db)
    from subagents.codex_runtime_events import settle_committed_tool_result

    for canonical in completed:
        await settle_committed_tool_result(
            live=live,
            thread_id=thread_id,
            identity=identity,
            canonical=canonical,
            bcast=bcast,
        )
