"""Record a Claude Code fork's usage-limit refusal on the login the fork ran on.

The same verdict a live Claude Code Turn records (``connections.claude_usage``
for a refusing ``rate_limit_event``, ``record_limited`` for a refusal named only
by its error result), so admission holds the account's inputs and the release
drive probes it at the reset. Only structured output is read (``fork_limit``).
"""

from __future__ import annotations

import logging

from core.fork_cc_result import ForkLimit, fork_limit
from infra.db_async import run_in_db

log = logging.getLogger(__name__)

#: Same cap as the terminal path's stored reason.
_REASON_LIMIT = 300


def _record(limit: ForkLimit, receipt, db=None) -> None:
    from automation.provider_reset_liveness import deadline
    from connections.claude_usage import PROVIDER, apply_rate_limit_event, names_plan_window
    from connections.harness_usage import record_limited
    from infra.database import write_transaction
    from subagents.provider_usage_limits import model_scope_from_limit

    with write_transaction(db, scope="fork_usage_limit") as connection:
        for info in limit.refusals:
            apply_rate_limit_event(info, receipt=receipt, db=connection)
        if limit.text is None or any(names_plan_window(info) for info in limit.refusals):
            return
        reset = deadline(limit.text)
        record_limited(
            provider=PROVIDER, reason=limit.text[:_REASON_LIMIT],
            resets_at=reset.isoformat() if reset else None,
            scope_model=model_scope_from_limit(limit.text), source="turn_error",
            receipt=receipt, db=connection,
        )


async def record_fork_limit(stdout: bytes, receipt) -> str | None:
    """Record any refusal in ``stdout``; return why one went unrecorded, else None.

    An unrecorded refusal leaves no verdict, so the fold fails visibly instead
    of being held: nothing would release a hold that has no verdict.
    """
    limit = fork_limit(stdout)
    if limit is None:
        return None
    if receipt is None:
        log.warning("Claude Code fork usage limit not recorded: the fork has no login receipt")
        return "usage-limit verdict not recorded: the fork has no login receipt"
    try:
        await run_in_db(_record, limit, receipt)
    except Exception as error:
        log.exception("Claude Code fork usage limit not recorded")
        return f"usage-limit verdict not recorded: {type(error).__name__}: {error}"
    return None
