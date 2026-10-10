"""Record a terminal harness refusal and settle expired limits before successor admission.

The caller owns terminal ordering and the supplied transaction; canonical usage
state remains exclusively in ``connections.harness_usage``. Each write here runs
in its own savepoint, so a failure never stops the Turn from ending, and none is
swallowed: when admission can no longer prove the account usable, the result
says so and the terminal fails the successor prefix closed and visibly.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from infra.database import write_transaction
from infra.product_events import emit_product_event
from infra.write_effects import after_commit

log = logging.getLogger(__name__)

#: Same cap as the stored ``check_error``.
_REASON_LIMIT = 300


@dataclass(frozen=True, slots=True)
class NotALimit:
    """The terminal carries no usage-limit refusal that names a harness account."""


@dataclass(frozen=True, slots=True)
class Recorded:
    """A refusal whose account verdict was written with the terminal."""


@dataclass(frozen=True, slots=True)
class Unrecorded:
    """A refusal whose verdict write raised and was rolled back."""

    error: str


TerminalLimit = NotALimit | Recorded | Unrecorded


@dataclass(frozen=True, slots=True)
class TerminalLimits:
    """What the terminal must do: its error text, and whether to fail closed.

    ``fail_closed`` is the reason the successor prefix is settled as failed
    instead of claimed; ``None`` admits the successor normally.
    """

    limit: TerminalLimit
    error: str | BaseException | None
    fail_closed: str | None = None


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _emit_after_commit(connection: sqlite3.Connection, action: str, thread_id: str, **properties) -> None:
    after_commit(connection, lambda: emit_product_event(
        "usage_limit", action, thread_id=thread_id, properties=properties,
    ))


def record_terminal_harness_limit(
    connection: sqlite3.Connection, thread_id: str, turn_id: str,
    error: str | BaseException | None,
) -> TerminalLimit:
    """Persist an explicit refusal as the account's verdict, typed by what happened."""
    if error is None:
        return NotALimit()
    from subagents.provider_usage_limits import is_provider_usage_or_quota_limit

    message = str(error)
    if not is_provider_usage_or_quota_limit(message):
        return NotALimit()
    account: list[str] = []
    try:
        with write_transaction(connection, scope="terminal_usage_limit"):
            written = _record(connection, turn_id, message, account)
    except Exception as failure:
        log.exception("terminal usage-limit verdict not recorded for thread %s", thread_id)
        _emit_after_commit(
            connection, "verdict_write_failed", thread_id,
            account_key=account[0] if account else None, error=_error_text(failure),
        )
        return Unrecorded(_error_text(failure))
    return Recorded() if written else NotALimit()


def _record(connection: sqlite3.Connection, turn_id: str, message: str, account: list[str]) -> bool:
    """Write the limited verdict; False when no harness account can be named."""
    from automation.provider_reset_liveness import deadline
    from connections.harness_usage import record_limited
    from llm.model_status import harness_connection_for_model
    from subagents.provider_usage_limits import model_scope_from_limit

    # The refusal belongs to the model that ran this Turn, not a later switch.
    row = connection.execute("SELECT model FROM active_turns WHERE id=?", (turn_id,)).fetchone()
    model = str(row["model"]) if row is not None else None
    provider = harness_connection_for_model(model, db=connection)
    if provider is None:
        return False
    from infra.vendor_login import read_turn_receipt

    # The refusal is the login the Turn ran on; without it, no account is named.
    receipt = read_turn_receipt(turn_id, db=connection)
    if receipt is None:
        log.warning('%s refusal on turn %s not recorded: the Turn has no login receipt', provider, turn_id)
        return False
    account.append(provider)
    reset = deadline(message)
    record_limited(
        provider=provider, reason=message[:_REASON_LIMIT],
        resets_at=reset.isoformat() if reset else None,
        scope_model=model_scope_from_limit(message), source="turn_error", receipt=receipt, db=connection,
    )
    return True


def settle_terminal_limits(
    connection: sqlite3.Connection, thread_id: str, turn_id: str, *,
    outcome: str, ended_at: str, error: str | BaseException | None,
) -> TerminalLimits:
    """Record the refusal (C1), then settle every expired scope of the Turn's model (C2)."""
    from connections.limit_settlement import Failed, settle_expired_scope

    limit = record_terminal_harness_limit(connection, thread_id, turn_id, error)
    fail_closed = None
    if isinstance(limit, Unrecorded):
        error = f"{error} (usage-limit verdict not recorded: {limit.error})"
        fail_closed = str(error)
    row = connection.execute(
        "SELECT model,started_at FROM active_turns WHERE id=?", (turn_id,),
    ).fetchone()
    if row is None:
        return TerminalLimits(limit, error, fail_closed)
    settlement = settle_expired_scope(
        model=str(row["model"]), thread_id=thread_id, started_at=str(row["started_at"]),
        settled_at=ended_at, completed=outcome == "completed" and error is None,
        normalized_refusal=isinstance(limit, Recorded), turn_id=turn_id, db=connection,
    )
    if isinstance(settlement, Failed):
        _emit_after_commit(
            connection, "settlement_failed", thread_id,
            error=settlement.error, marked=settlement.marked,
        )
        if not settlement.marked and fail_closed is None:
            fail_closed = str(error) if error is not None else settlement.reason or settlement.error
    return TerminalLimits(limit, error, fail_closed)
