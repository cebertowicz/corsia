"""Fence a thread's family, stop it, then delete it in one write or refuse it whole."""

from __future__ import annotations

from automation.managed_process_events import (
    abort_thread_close,
    finish_thread_close,
    prepare_thread_close,
)
from core.thread_family import delete_thread_family, thread_family_ids
from host_access.processes import ProcessCleanupError
from infra import database
from infra.admission_fence import Fence, fence
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from plans import public
from thread import runtime_boundary, transcript_order
from thread.model_state import ThreadQuiescenceError

BoundaryVersions = dict[str, tuple[int, int, int]]


def fence_owned_family(thread_id: str, user_id: str, held: Fence) -> list[str] | None:
    """Fence the family deleting ``thread_id`` removes; None when it is not theirs.

    The fence is set inside a write, so every admission that read it unset has
    committed and every later one sees it. Returns ``thread_family_ids``' order.
    """
    with database.write_transaction(None, scope="fence_thread_family") as db:
        if get_thread_for_user(thread_id, user_id, db=db) is None:
            return None
        family = thread_family_ids(db, thread_id)
        held.add(family)
        return family


def family_versions(family: list[str]) -> BoundaryVersions:
    """Snapshot each fenced, stopped member's boundary."""
    db = database.get_db()
    try:
        return {
            member_id: transcript_order.destructive_boundary_version(db, member_id)
            for member_id in family
        }
    finally:
        db.close()


async def quiesce_thread_family(members: list[str], user_id: str) -> None:
    """Stop each member in the order given."""
    for member_id in members:
        await runtime_boundary.quiesce_thread_runtimes(member_id, user_id=user_id)


def delete_thread_rows(
    thread_id: str,
    user_id: str,
    expected_versions: BoundaryVersions,
    drives: list[str] | None = None,
) -> str:
    """Delete the family ``core.thread_family`` defines, or refuse it whole.

    Refuses with ``turn_raced`` when the family is not the snapshotted one or
    any member's boundary moved.
    """
    with (
        database.write_transaction(None, scope="thread_row_delete") as db,
        public.collect_wake_recipients(db, drives),
    ):
        if get_thread_for_user(thread_id, user_id, db=db) is None:
            return "thread_not_found"
        family_ids = thread_family_ids(db, thread_id)
        if set(family_ids) != set(expected_versions) or not all(
            transcript_order.destructive_boundary_is_clear(
                db, member_id, expected_versions[member_id],
            )
            for member_id in family_ids
        ):
            return "turn_raced"
        delete_thread_family(db, family_ids)
        return "ok"


async def _stop_then_delete(
    thread_id: str, user_id: str, held: Fence, drives: list[str],
) -> str:
    """Stop the fenced family from the thread down, then delete it.

    A stopped member cannot spawn, so each subagent a running member spawns
    meanwhile is found by the re-read below it, fenced and stopped, until a
    re-read finds no one new.
    """
    family = await run_in_db(fence_owned_family, thread_id, user_id, held)
    stopped: set[str] = set()
    while family is not None and (
        newcomers := [m for m in reversed(family) if m not in stopped]
    ):
        await quiesce_thread_family(newcomers, user_id)
        stopped.update(newcomers)
        family = await run_in_db(fence_owned_family, thread_id, user_id, held)
    if family is None:
        return "thread_not_found"
    try:
        close_token = await prepare_thread_close(family)
    except ProcessCleanupError as exc:
        raise ThreadQuiescenceError(str(exc)) from exc
    committed = False
    try:
        versions = await run_in_db(family_versions, family)
        status = await run_in_db(
            delete_thread_rows,
            thread_id,
            user_id,
            versions,
            drives,
        )
        if status != "ok":
            await abort_thread_close(close_token)
            return status
        committed = True
        await finish_thread_close(close_token)
        return status
    except BaseException:
        if not committed:
            await abort_thread_close(close_token)
        raise


async def delete_owned_thread(thread_id: str, user_id: str, drives: list[str]) -> str:
    """Fence, stop and delete one owned thread's family.

    A fenced member's queued input cannot start a Turn, so a delete that does
    not finish hands the family it fenced to ``drives`` once the fence lifts.
    """
    status, fenced = "turn_raced", set()
    try:
        with fence() as held:
            fenced = held.held
            status = await _stop_then_delete(thread_id, user_id, held, drives)
    finally:
        if status != "ok":
            drives.extend(sorted(fenced))
    return status
