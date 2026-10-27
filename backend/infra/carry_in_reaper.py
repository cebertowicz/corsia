"""Reconcile carry-in work a restart or a fault stranded.

A wake of ``infra.carry_in_drain`` ends the claims no running wake owns and
deletes the eval threads whose case already holds a terminal fact.
"""

from __future__ import annotations

from loguru import logger

from core import carry_in_facts as facts
from core.context_projection import C1_THREAD_PREFIX


def settle_interrupted(db) -> int:
    """End every claim no wake is running: an earlier boot's was interrupted by its restart;
    this boot's lost its own terminal write to a fault (one wake runs at a time per process)."""
    from core.turn_core import BOOT_EPOCH
    from infra.time import utc_now_iso

    stale = facts.open_facts(db, claimed=True)
    now = utc_now_iso()
    for fact in stale:
        reason = "error" if facts.claim_boot(db, fact) == BOOT_EPOCH else "interrupted"
        facts.record_terminal(db, fact, now, facts.COVERAGE, reason=reason)
    return len(stale)


def stranded_threads(db) -> list[tuple[str, str]]:
    """Eval threads (id, user) whose case already has a terminal fact."""
    rows = db.execute(
        "SELECT id, user_id FROM threads WHERE id LIKE ? ESCAPE '\\'",
        (C1_THREAD_PREFIX.replace("_", "\\_") + "%",),
    ).fetchall()
    out = []
    for row in rows:
        prefix = str(row[0])[len(C1_THREAD_PREFIX):].split("-", 1)[0]
        if prefix and facts.has_terminal_with_prefix(db, prefix):
            out.append((str(row[0]), str(row[1] or "")))
    return out


async def delete_threads(threads: list[tuple[str, str]]) -> None:
    from api.thread_deletion import delete_owned_thread

    for thread_id, user_id in threads:
        try:
            status = await delete_owned_thread(thread_id, user_id, [])
            if status != "ok":
                logger.warning("carry-in eval thread {} not deleted: {}", thread_id, status)
        except Exception:
            logger.opt(exception=True).warning("carry-in eval thread {} not deleted", thread_id)
