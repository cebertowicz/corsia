"""Usage-only recovery checks for CLI harness accounts.

The model picker triggers these checks only when it opens. Each account is
checked through its own usage-only control read, with no catalog discovery or
model turn. Added Claude accounts use their captured source home; Codex retains
its account-bound app-server path. Each account has one in-memory check at a
time and no more than one start per five minutes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from connections import harness_usage
from connections.usage_windows import parse_time
from connections.vendor_accounts import VENDORS
from infra.db_async import run_in_db

log = logging.getLogger(__name__)
_RECHECK_SECONDS = 300.0
#: Keyed by account id.
_CHECKS: dict[str, asyncio.Task[None]] = {}
_LAST_STARTED: dict[str, float] = {}
#: Accounts signed in while a read for the prior login was running; read again once it ends.
_NEW_LOGIN_PENDING: set[str] = set()


def is_checking(account_id: str) -> bool:
    """Whether this process is currently rechecking one harness account."""
    task = _CHECKS.get(account_id)
    return task is not None and not task.done()


def limited_accounts(*, db=None) -> list[tuple[str, str]]:
    """(provider, account id) of every current login that needs the canonical control read."""
    from connections.account_receipts import account_receipt
    from connections.vendor_accounts import list_accounts
    from infra.database import get_db

    connection = db or get_db()
    try:
        limited = []
        for account in list_accounts(db=connection):
            if account['provider'] not in VENDORS or account_receipt(account) is None:
                continue
            usage = harness_usage.stored_usage(provider=account['provider'], account_id=account['id'], db=connection)
            projection = harness_usage.account_usage(usage, provider=account['provider'])
            reset_passed = any(
                isinstance(window, dict)
                and (reset := parse_time(window.get("resets_at"))) is not None
                and reset <= datetime.now(timezone.utc)
                for window in projection.get("windows", ())
            )
            if not projection.get("usable", True) or reset_passed:
                limited.append((account['provider'], account['id']))
        return limited
    finally:
        if db is None:
            connection.close()


async def schedule_limited_usage_rechecks() -> None:
    """Schedule one control read per limited or expired visible account when the picker opens."""
    for provider, account_id in await run_in_db(limited_accounts):
        _schedule(provider, account_id)


def schedule_account_check(provider: str, account_id: str, *, new_login: bool = False) -> None:
    """Request the account's canonical bounded control read from any thread.

    The account-wide five-minute throttle and single-flight guard apply to every
    control read, except that a new login's first read is never throttled by the
    prior login's: its stored readings were just marked stale.
    """
    import infra.app_loop as app_loop

    async def _start() -> None:
        if new_login:
            _LAST_STARTED.pop(account_id, None)
            if is_checking(account_id):
                # The running read is fenced to the prior login and cannot settle this one.
                _NEW_LOGIN_PENDING.add(account_id)
                return
        _schedule(provider, account_id)

    try:
        app_loop.submit(_start())
    except app_loop.AppLoopUnavailable:
        log.warning("%s usage read not scheduled after sign-in: no live event loop", provider)


def _schedule(provider: str, account_id: str) -> None:
    if is_checking(account_id):
        return
    now = time.monotonic()
    if now - _LAST_STARTED.get(account_id, float("-inf")) < _RECHECK_SECONDS:
        return
    _LAST_STARTED[account_id] = now
    _CHECKS[account_id] = asyncio.create_task(_check(provider, account_id))


def _capture(provider: str, account_id: str, *, db=None):
    """Capture the current account context once before a bounded usage read."""
    from connections.account_discovery_types import capture_discovery_context

    return capture_discovery_context(provider, account_id=account_id, db=db)


async def _check(provider: str, account_id: str) -> None:
    receipt = None
    try:
        context = await run_in_db(_capture, provider, account_id)
        if context is None or context.receipt is None:
            return  # No current login to read for: the next sign-in schedules its own check.
        receipt = context.receipt
        if not context.is_default and provider != "claude-code":
            await _check_added(provider, receipt)
            return
        binary = await _binary_for(provider)
        if binary is None:
            await _check_failed(provider, receipt, "Harness connection is unavailable")
        elif provider == "claude-code":
            await _check_claude(binary, receipt, **({} if context.is_default else {'context': context}))
        else:
            await _check_codex(binary, receipt)
    except Exception as exc:
        log.warning("%s usage recheck failed for account %s: %s", provider, account_id, exc)
        await _check_failed(provider, receipt, f"{provider} usage read failed: {exc}")
    finally:
        _CHECKS.pop(account_id, None)
        if account_id in _NEW_LOGIN_PENDING:
            _NEW_LOGIN_PENDING.discard(account_id)
            _LAST_STARTED.pop(account_id, None)
            _schedule(provider, account_id)
        from infra.state.ws import notify_models_changed

        notify_models_changed()


async def _binary_for(provider: str) -> str | None:
    from connections import harness_models

    binary, unavailable = await harness_models._db_call(
        harness_models._provider_binary, provider,
    )
    return None if unavailable else binary


async def _check_failed(provider: str, receipt, error: str) -> None:
    await run_in_db(harness_usage.record_check_failed, error=error, provider=provider, receipt=receipt)


async def _check_added(provider: str, receipt) -> None:
    """An added account's usage is read by its own refresh, in its own home and login."""
    from connections import harness_models

    result = await harness_models.refresh_provider(provider, reason="usage-recheck", account_id=receipt.account_id,
                                                   expected_receipt=receipt)
    # A refresh that never read usage leaves the last verdict standing and says so, as any failed check does.
    if isinstance(result, dict):
        failed, cause = not result.get("ok"), result.get("error")
    else:
        failed, cause = result.status != "ready", result.cause
    if failed and cause != "account_refresh_superseded":
        await _check_failed(provider, receipt, f"{provider} usage read failed: {cause or 'refresh did not finish'}")


async def _check_codex(binary: str, receipt) -> None:
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_usage_observer import observe_account_limits

    session = CodexAppServerSession(binary=binary, task=None, control_only=True)
    session.vendor_account_bound = True
    session.vendor_account_id = receipt.account_id
    session.vendor_account_receipt = receipt
    try:
        await session.start()
        await observe_account_limits(session)
    finally:
        await session.close(timeout=10.0)


async def _check_claude(binary: str, receipt, *, context=None) -> None:
    from connections import claude_usage
    from connections.claude_discovery import _control_responses, run_claude_control

    frames = [
        {"type": "control_request", "request_id": "corsia-usage",
         "request": {"subtype": "get_usage", "skip_behaviors": True}},
        {"type": "control_request", "request_id": "corsia-usage-end",
         "request": {"subtype": "end_session", "reason": "usage_check_complete"}},
    ]
    stdout = await run_claude_control(
        binary, frames, operation="get_usage",
        **({'context': context} if context is not None else {}),
    )
    response = _control_responses(stdout).get("corsia-usage")
    report = response.get("response") if response and response.get("subtype") == "success" else None
    await run_in_db(claude_usage.apply_usage_response, report, receipt=receipt)
